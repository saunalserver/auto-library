"""CLI for dedup: scan / report / protect / unprotect / trash / restore / purge / history.

Reversibility model:
  trash   → move to ~/music-trash/<YYYY-MM-DD>/<original-path>
  restore → move it back
  purge   → permanent rm, requires --yes, refuses protected paths
"""
from __future__ import annotations

import argparse
import json as jsonlib
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

from dedup_lib import init_dedup_schema
import dedup_scan
import dedup_state

DEFAULT_DB = "database/monitor.db"


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    init_dedup_schema(conn)
    return conn


def _add_db_arg(p):
    p.add_argument("--db", default=DEFAULT_DB,
                   help="Path to monitor.db (default: database/monitor.db)")


def _trash_path_for(filepath: Path, trash_root: Path) -> Path:
    """Compute trash destination: trash_root/<YYYY-MM-DD>/<full-original-path>."""
    date = datetime.now().strftime("%Y-%m-%d")
    # Strip leading slash so we can rglob under trash_root
    relative = str(filepath).lstrip("/")
    return trash_root / date / relative


def cmd_trash(args) -> int:
    src = Path(args.filepath_or_id).resolve()
    if not src.exists():
        # Maybe it's a finding ID — try lookup
        conn = _connect(args.db)
        row = conn.execute("SELECT filepath FROM dedup_findings WHERE id = ?",
                           (args.filepath_or_id,)).fetchone()
        conn.close()
        if row is None:
            print(f"ERROR: not a path or finding ID: {args.filepath_or_id}", file=sys.stderr)
            return 1
        src = Path(row["filepath"]).resolve()

    trash_root = Path(args.trash_root).expanduser()
    trash_root.mkdir(parents=True, exist_ok=True)
    dest = _trash_path_for(src, trash_root)
    dest.parent.mkdir(parents=True, exist_ok=True)

    conn = _connect(args.db)
    # Refuse protected
    prot = conn.execute("SELECT 1 FROM dedup_protections WHERE filepath = ?",
                        (str(src),)).fetchone()
    if prot:
        print(f"ERROR: file is protected: {src}", file=sys.stderr)
        conn.close()
        return 2

    shutil.move(str(src), str(dest))
    conn.execute(
        "UPDATE dedup_findings SET status='trash' WHERE filepath = ?", (str(src),)
    )
    conn.execute(
        "INSERT INTO dedup_log (filepath, action, when_at, actor, details) "
        "VALUES (?, 'trash', datetime('now'), ?, ?)",
        (str(src), args.actor, str(dest)),
    )
    conn.commit()
    conn.close()
    print(f"Trashed: {src} → {dest}")
    return 0


def cmd_restore(args) -> int:
    src = Path(args.filepath_or_id).resolve()
    trash_root = Path(args.trash_root).expanduser()
    # Find the file in trash — search by suffix matching original path
    relative = str(src).lstrip("/")
    candidates = list(trash_root.rglob(relative))
    if not candidates:
        print(f"ERROR: not found in trash: {src}", file=sys.stderr)
        return 1
    if len(candidates) > 1:
        # Pick the most recent
        candidates.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    found = candidates[0]

    src.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(found), str(src))
    conn = _connect(args.db)
    conn.execute(
        "UPDATE dedup_findings SET status='pending' WHERE filepath = ?", (str(src),)
    )
    conn.execute(
        "INSERT INTO dedup_log (filepath, action, when_at, actor, details) "
        "VALUES (?, 'restore', datetime('now'), ?, NULL)",
        (str(src), args.actor),
    )
    conn.commit()
    conn.close()
    print(f"Restored: {found} → {src}")
    return 0


def cmd_protect(args) -> int:
    path = Path(args.filepath).resolve()
    if not path.exists():
        print(f"ERROR: path does not exist: {path}", file=sys.stderr)
        return 1
    conn = _connect(args.db)
    conn.execute(
        "INSERT OR REPLACE INTO dedup_protections (filepath, reason, added_at, added_by) "
        "VALUES (?, ?, datetime('now'), ?)",
        (str(path), args.reason or "(no reason given)", args.actor),
    )
    # Update any pending findings on this path
    conn.execute(
        "UPDATE dedup_findings SET status='protected' WHERE filepath = ? AND status='pending'",
        (str(path),),
    )
    conn.execute(
        "INSERT INTO dedup_log (filepath, action, when_at, actor, details) "
        "VALUES (?, 'protect', datetime('now'), ?, ?)",
        (str(path), args.actor, args.reason),
    )
    conn.commit()
    conn.close()
    print(f"Protected: {path}")
    return 0


def cmd_unprotect(args) -> int:
    path = str(Path(args.filepath).resolve())
    conn = _connect(args.db)
    conn.execute("DELETE FROM dedup_protections WHERE filepath = ?", (path,))
    conn.execute(
        "UPDATE dedup_findings SET status='pending' WHERE filepath = ? AND status='protected'",
        (path,),
    )
    conn.execute(
        "INSERT INTO dedup_log (filepath, action, when_at, actor, details) "
        "VALUES (?, 'unprotect', datetime('now'), ?, NULL)",
        (path, args.actor),
    )
    conn.commit()
    conn.close()
    print(f"Unprotected: {path}")
    return 0


def cmd_scan(args) -> int:
    conn = _connect(args.db)
    n = dedup_scan.scan_library(conn, Path(args.root))
    findings = dedup_state.compare_library(conn)
    print(f"Scanned {n} files, {findings} new findings")
    conn.close()
    return 0


def cmd_report(args) -> int:
    conn = _connect(args.db)
    cur = conn.cursor()
    where = []
    params = []
    if args.status:
        where.append("status = ?")
        params.append(args.status)
    if args.artist:
        where.append("artist LIKE ?")
        params.append(f"%{args.artist}%")
    sql = "SELECT * FROM dedup_findings"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY added_at DESC"
    cur.execute(sql, params)
    rows = cur.fetchall()
    if args.json:
        jsonlib.dump([dict(r) for r in rows], sys.stdout, indent=2, default=str)
        print()
    else:
        if not rows:
            print("No findings.")
        for r in rows:
            print(f"  [{r['id']}] {r['status']:9} sim={r['similarity']:.2f} "
                  f"{r['artist']} - {r['album']} - {r['title']}")
            print(f"        path:    {r['filepath']}")
            print(f"        matches: {r['matched_path']}")
    conn.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="dedup_tool")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_scan = sub.add_parser("scan", help="Fingerprint library + run comparison")
    _add_db_arg(p_scan)
    p_scan.add_argument("--root", default="/mnt/photos/flac_music")
    p_scan.set_defaults(func=cmd_scan)

    p_protect = sub.add_parser("protect", help="Mark a file as never-to-delete")
    _add_db_arg(p_protect)
    p_protect.add_argument("filepath")
    p_protect.add_argument("reason", nargs="?")
    p_protect.add_argument("--actor", default="user")
    p_protect.set_defaults(func=cmd_protect)

    p_trash = sub.add_parser("trash", help="Move a file to trash (reversible)")
    _add_db_arg(p_trash)
    p_trash.add_argument("filepath_or_id", help="Filepath or dedup_findings.id")
    p_trash.add_argument("--trash-root", default="~/music-trash",
                         help="Trash directory (default: ~/music-trash)")
    p_trash.add_argument("--actor", default="user")
    p_trash.set_defaults(func=cmd_trash)

    p_restore = sub.add_parser("restore", help="Reverse a trash action")
    _add_db_arg(p_restore)
    p_restore.add_argument("filepath_or_id")
    p_restore.add_argument("--trash-root", default="~/music-trash")
    p_restore.add_argument("--actor", default="user")
    p_restore.set_defaults(func=cmd_restore)

    p_unprotect = sub.add_parser("unprotect", help="Remove protection")
    _add_db_arg(p_unprotect)
    p_unprotect.add_argument("filepath")
    p_unprotect.add_argument("--actor", default="user")
    p_unprotect.set_defaults(func=cmd_unprotect)

    p_report = sub.add_parser("report", help="Print dedup findings")
    _add_db_arg(p_report)
    p_report.add_argument("--status", default="pending",
                          help="Filter by status (default: pending; pass '' for all)")
    p_report.add_argument("--artist", help="Filter by artist (substring)")
    p_report.add_argument("--json", action="store_true", help="JSON output")
    p_report.set_defaults(func=cmd_report)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

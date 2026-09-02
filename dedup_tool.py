"""CLI for dedup: scan / report / protect / unprotect / trash / restore / purge / history.

Reversibility model:
  trash   → move to ~/music-trash/<YYYY-MM-DD>/<original-path>
  restore → move it back
  purge   → permanent rm, requires --yes, refuses protected paths
"""
from __future__ import annotations

import argparse
import json as jsonlib
import re
import shutil
import sqlite3
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

from dedup_lib import init_dedup_schema
import dedup_scan
import dedup_state

DEFAULT_DB = "database/monitor.db"


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=60)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.OperationalError:
        pass
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


def _parse_duration(s: str) -> int:
    """Parse '30d' / '12h' / '60m' to seconds."""
    m = re.match(r"^(\d+)([dhm])$", s)
    if not m:
        raise ValueError(f"Bad duration: {s}")
    n, unit = int(m.group(1)), m.group(2)
    return n * {"d": 86400, "h": 3600, "m": 60}[unit]


def cmd_purge(args) -> int:
    if not args.yes:
        print("ERROR: purge requires --yes (permanent delete)", file=sys.stderr)
        return 1
    # Extract day count for the min-7d guard (only relevant for 'd' inputs)
    m = re.match(r"^(\d+)d$", args.older_than)
    min_days = int(m.group(1)) if m else 0
    if min_days and min_days < 7:
        print(f"ERROR: --older-than must be at least 7d (got {args.older_than})", file=sys.stderr)
        return 1

    threshold_seconds = _parse_duration(args.older_than)
    now = time.time()
    trash_root = Path(args.trash_root).expanduser()
    if not trash_root.exists():
        print(f"Trash root does not exist: {trash_root}")
        return 0

    conn = _connect(args.db)
    count = 0
    for f in trash_root.rglob("*"):
        if not f.is_file():
            continue
        if now - f.stat().st_mtime < threshold_seconds:
            continue
        # Refuse if protected
        # Reconstruct original path: trash_root/<date>/<original-path>
        # original path = everything after trash_root/<date>/
        parts = f.relative_to(trash_root).parts
        if len(parts) < 3:
            original = "/" + "/".join(parts[1:])  # unusual layout
        else:
            original = "/" + "/".join(parts[2:])
        prot = conn.execute("SELECT 1 FROM dedup_protections WHERE filepath = ?",
                            (original,)).fetchone()
        if prot:
            print(f"SKIP (protected): {original}")
            continue
        f.unlink()
        conn.execute(
            "INSERT INTO dedup_log (filepath, action, when_at, actor, details) "
            "VALUES (?, 'purge', datetime('now'), ?, NULL)",
            (original, args.actor),
        )
        count += 1
    conn.execute(
        "UPDATE dedup_findings SET status='deleted' WHERE status='trash'"
    )
    conn.commit()
    conn.close()
    print(f"Purged {count} files from trash")
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
    import logging
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", stream=sys.stdout)
    conn = _connect(args.db)
    try:
        n = dedup_scan.scan_library(conn, Path(args.root), workers=args.workers, prune=True)
    except dedup_scan.LibraryUnavailable as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        conn.close()
        return 2
    findings = dedup_state.compare_library(conn)
    print(f"Fingerprinted {n} files, {findings} new findings")
    conn.close()
    return 0


def cmd_history(args) -> int:
    conn = _connect(args.db)
    cur = conn.cursor()
    cur.execute("SELECT * FROM dedup_log ORDER BY when_at DESC LIMIT ?", (args.limit,))
    for r in cur.fetchall():
        print(f"  {r['when_at']}  {r['action']:10}  {r['actor']:8}  {r['filepath']}")
        if r["details"]:
            print(f"        → {r['details']}")
    conn.close()
    return 0


def cmd_ntfy_summary(args) -> int:
    conn = _connect(args.db)
    cur = conn.execute(
        "SELECT COUNT(*) as n, COALESCE(SUM(size_bytes), 0) as bytes "
        "FROM dedup_findings WHERE status = 'pending'"
    )
    row = cur.fetchone()
    n = row["n"]
    bytes_reclaimable = row["bytes"]
    # Each duplicate pair is stored as two rows (one per file); report pairs.
    msg = (f"Dedup: {n // 2} duplicate pair(s) pending review, {bytes_reclaimable / 2 / 1e9:.1f} GB reclaimable. "
           f"Review: dedup_tool.py report")
    print(msg)
    if n == 0:
        return 0
    if args.ntfy_url:
        try:
            req = urllib.request.Request(args.ntfy_url, data=msg.encode("utf-8"), method="POST")
            req.add_header("Title", "Music Dedup Status")
            req.add_header("Tags", "headphones,magnifying_glass")
            urllib.request.urlopen(req, timeout=5)
        except Exception as e:
            print(f"(ntfy failed: {e})", file=sys.stderr)
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
    p_scan.add_argument("--workers", type=int, default=4, help="parallel fpcalc processes")
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

    p_purge = sub.add_parser("purge", help="Permanently delete old trash")
    _add_db_arg(p_purge)
    p_purge.add_argument("--trash-root", default="~/music-trash")
    p_purge.add_argument("--older-than", default="30d",
                         help="Only purge files older than this (e.g. 30d, 12h, 60m)")
    p_purge.add_argument("--yes", action="store_true",
                         help="Required to actually delete (otherwise dry-run report)")
    p_purge.add_argument("--actor", default="user")
    p_purge.set_defaults(func=cmd_purge)

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

    p_history = sub.add_parser("history", help="Show dedup_log entries")
    _add_db_arg(p_history)
    p_history.add_argument("--limit", type=int, default=50)
    p_history.set_defaults(func=cmd_history)

    p_ntfy = sub.add_parser("ntfy-summary", help="Send pending-count to ntfy")
    _add_db_arg(p_ntfy)
    p_ntfy.add_argument("--ntfy-url", default="http://localhost:8093/music-dedup")
    p_ntfy.set_defaults(func=cmd_ntfy_summary)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

"""CLI for dedup: scan / report / protect / unprotect / trash / restore / purge / history.

Reversibility model:
  trash   → move to ~/music-trash/<YYYY-MM-DD>/<original-path>
  restore → move it back
  purge   → permanent rm, requires --yes, refuses protected paths
"""
from __future__ import annotations

import argparse
import json as jsonlib
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

from dedup_lib import init_dedup_schema

DEFAULT_DB = "database/monitor.db"


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    init_dedup_schema(conn)
    return conn


def _add_db_arg(p):
    p.add_argument("--db", default=DEFAULT_DB,
                   help="Path to monitor.db (default: database/monitor.db)")


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

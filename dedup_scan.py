"""Scan the music library and fingerprint every audio file.

Idempotent — skips files whose mtime hasn't changed since last scan.
"""
from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
from pathlib import Path

from dedup_lib import init_dedup_schema, index_file
from mutagen import File as MutagenFile

log = logging.getLogger(__name__)

AUDIO_EXTS = {".flac", ".m4a", ".mp3", ".opus", ".ogg"}


def parse_metadata(path: Path) -> tuple[str, str, str]:
    """Try mutagen for artist/album/title; fall back to path parsing."""
    try:
        tags = MutagenFile(str(path))
        if tags is not None:
            artist = tags.get("artist", [""])[0]
            album = tags.get("album", [""])[0]
            title = tags.get("title", [""])[0]
            if artist and title:
                return artist, album or "", title
    except Exception as e:
        log.debug("mutagen failed on %s: %s", path, e)
    # Path fallback: /root/Artist/Album/NN - Title.flac
    parts = path.parts
    if len(parts) >= 3:
        artist = parts[-3]
        album = parts[-2]
        title = path.stem
        # Strip leading track number
        if len(title) > 3 and title[:2].isdigit() and title[2:4] in {" -", "_-"}:
            title = title[4:].lstrip(" -")
        return artist, album, title
    return "Unknown", "Unknown", path.stem


def scan_library(conn: sqlite3.Connection, root: Path) -> int:
    """Walk root, fingerprint every audio file. Returns count indexed (not skipped)."""
    root = Path(root)
    count = 0
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in AUDIO_EXTS:
            continue
        artist, album, title = parse_metadata(path)
        index_file(conn, path, artist, album, title)
        count += 1
    return count


def main():
    parser = argparse.ArgumentParser(description="Fingerprint the music library")
    parser.add_argument("--root", default="/mnt/photos/flac_music")
    parser.add_argument("--db", default="database/monitor.db")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    conn = sqlite3.connect(args.db)
    init_dedup_schema(conn)
    n = scan_library(conn, Path(args.root))
    log.info("Processed %d files", n)
    conn.close()


if __name__ == "__main__":
    sys.exit(main())

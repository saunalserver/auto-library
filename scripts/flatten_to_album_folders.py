#!/usr/bin/env python3
"""
One-shot cleanup: move flat files in /mnt/photos/flac_music/ root into proper
{album_artist}/{album}/ subfolders, using Navidrome's tag metadata as the
source of truth.

Background: tiddl's album template was missing the {album_artist}/{album}/
prefix for several months, so 206 downloads landed in the music root instead
of in artist folders. Template was fixed on 2026-07-19; this script handles
the existing backlog.

Usage:
    python3 flatten_to_album_folders.py --dry-run   # show what would move
    python3 flatten_to_album_folders.py             # actually move
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

MUSIC_ROOT = Path("/mnt/photos/flac_music")
NAVIDROME_CONTAINER = "navidrome"
NAVIDROME_DB = "/data/navidrome.db"


def fetch_flat_files() -> list[tuple[str, str, str]]:
    """Return list of (filename, album_artist, album) for files sitting in MUSIC_ROOT."""
    cmd = [
        "docker", "exec", NAVIDROME_CONTAINER,
        "sqlite3", NAVIDROME_DB,
        "SELECT path, album_artist, album FROM media_file "
        "WHERE path NOT LIKE '%/%' "
        "  AND album != '[Unknown Album]' "
        "  AND album_artist != '[Unknown Artist]' "
        "ORDER BY album_artist, album",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=40)
    if result.returncode != 0:
        sys.exit(f"Navidrome query failed: {result.stderr.strip()}")

    rows = []
    for line in result.stdout.splitlines():
        parts = line.split("|", 2)
        if len(parts) == 3:
            rows.append(tuple(parts))
    return rows


def sanitize(name: str) -> str:
    """Strip path separators and other filesystem-unsafe chars."""
    return name.replace("/", "_").replace("\x00", "").strip() or "_"


def plan_moves(rows: list[tuple[str, str, str]]) -> dict[Path, list[Path]]:
    """Group source files by their target folder."""
    groups: dict[Path, list[Path]] = defaultdict(list)
    for filename, artist, album in rows:
        target_dir = MUSIC_ROOT / sanitize(artist) / sanitize(album)
        src = MUSIC_ROOT / filename
        # Skip if file no longer exists (already moved or deleted)
        if src.exists():
            groups[target_dir].append(src)
    return groups


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="show what would move, don't actually move")
    args = ap.parse_args()

    rows = fetch_flat_files()
    print(f"Found {len(rows)} flat files in Navidrome's index")

    groups = plan_moves(rows)
    total_files = sum(len(v) for v in groups.values())
    print(f"Grouped into {len(groups)} target folders ({total_files} files to move)")
    print()

    for target_dir in sorted(groups):
        print(f"{target_dir}/")
        for src in sorted(groups[target_dir]):
            print(f"  <- {src.name}")

    if args.dry_run:
        print(f"\n[dry-run] No files moved. Re-run without --dry-run to execute.")
        return 0

    print(f"\nMoving {total_files} files into {len(groups)} folders...")
    moved = 0
    errors = []
    for target_dir, sources in groups.items():
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
        except Exception as exc:
            errors.append(f"mkdir {target_dir}: {exc}")
            continue
        for src in sources:
            dst = target_dir / src.name
            try:
                # Safety: never overwrite an existing file
                if dst.exists():
                    errors.append(f"skip (exists): {dst}")
                    continue
                shutil.move(str(src), str(dst))
                moved += 1
            except Exception as exc:
                errors.append(f"move {src} -> {dst}: {exc}")

    print(f"Moved: {moved}")
    if errors:
        print(f"Errors ({len(errors)}):")
        for e in errors[:20]:
            print(f"  {e}")

    print("\nTriggering Navidrome rescan so the new paths are indexed...")
    subprocess.run(
        ["docker", "exec", NAVIDROME_CONTAINER, "navidrome", "scan", "--datafolder", "/data", "--musicfolder", "/music"],
        check=False,
    )
    return 0 if not errors else 1


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Scan music library for split-folder issues (Issues 2 + 3 from tidal audit).

Reports:
  - Artist folders that differ only by case (e.g. "Charli xcx" vs "Charli XCX")
  - Album folders whose names contain special chars tiddl now sanitizes
    (legacy from before sanitizeString existed)
  - Album folders that look like sanitized variants of each other
    (e.g. "RUIN: It's Not Just Music" alongside "RUIN Its Not Just Music")

DRY RUN ONLY. Prints a report. Does not move or delete anything.
"""
from __future__ import annotations
import re
from collections import defaultdict
from pathlib import Path

MUSIC_ROOT = Path("/mnt/photos/flac_music")

# Same chars tiddl's sanitizeString strips: \/:"*?<>|
SANITIZE_PATTERN = re.compile(r'[\\/:"*?<>|]+')


def sanitize(name: str) -> str:
    """Mimic tiddl's sanitizeString: strip special chars, leave rest alone."""
    return SANITIZE_PATTERN.sub("", name)


def count_audio(folder: Path) -> int:
    if not folder.is_dir():
        return 0
    return sum(1 for _ in folder.glob("*.flac")) + sum(1 for _ in folder.glob("*.m4a"))


def find_case_split_artists() -> list[list[Path]]:
    """Find artist folders that normalize to the same lowercase name."""
    groups: dict[str, list[Path]] = defaultdict(list)
    for d in MUSIC_ROOT.iterdir():
        if d.is_dir():
            groups[d.name.lower().strip()].append(d)
    return [sorted(g) for g in groups.values() if len(g) > 1]


def find_albums_with_special_chars() -> list[Path]:
    """Album folders whose name still contains a char tiddl would now strip."""
    out = []
    for artist_dir in MUSIC_ROOT.iterdir():
        if not artist_dir.is_dir():
            continue
        for album_dir in artist_dir.iterdir():
            if not album_dir.is_dir():
                continue
            if SANITIZE_PATTERN.search(album_dir.name):
                out.append(album_dir)
    return sorted(out)


def find_sanitized_album_variants() -> list[list[Path]]:
    """Album folders under same artist whose sanitized names match.

    E.g. "RUIN: It's Not Just Music" and "RUIN Its Not Just Music" both
    sanitize to "RUIN Its Not Just Music".
    """
    out = []
    for artist_dir in MUSIC_ROOT.iterdir():
        if not artist_dir.is_dir():
            continue
        groups: dict[str, list[Path]] = defaultdict(list)
        for album_dir in artist_dir.iterdir():
            if not album_dir.is_dir():
                continue
            key = sanitize(album_dir.name).strip()
            groups[key].append(album_dir)
        for group in groups.values():
            if len(group) > 1:
                out.append(sorted(group))
    return out


def main() -> int:
    if not MUSIC_ROOT.exists():
        print(f"ERROR: MUSIC_ROOT does not exist: {MUSIC_ROOT}")
        return 1

    print("=" * 80)
    print("FOLDER SPLIT SCAN (DRY RUN - no changes)")
    print(f"Music root: {MUSIC_ROOT}")
    print("=" * 80)

    # --- Issue 3: artist casing splits ---
    print("\n--- ARTIST CASING SPLITS ---\n")
    case_splits = find_case_split_artists()
    if not case_splits:
        print("  None found.")
    else:
        for group in case_splits:
            total_audio = sum(count_audio(d) for d in group)
            print(f"  Artist (case-insensitive): {group[0].name.lower()}")
            for d in group:
                n = count_audio(d)
                subdirs = sum(1 for _ in d.iterdir() if _.is_dir())
                print(f"    {d.name!r:40s}  {n:5d} audio files, {subdirs} subfolders")
            print()

    # --- Issue 2a: album folders with unsanitized special chars ---
    print("--- ALBUM FOLDERS WITH LEGACY UNSANITIZED CHARS ---\n")
    special = find_albums_with_special_chars()
    if not special:
        print("  None found.")
    else:
        for album_dir in special:
            n = count_audio(album_dir)
            sanitized = sanitize(album_dir.name)
            print(f"  {album_dir.parent.name}/{album_dir.name}")
            print(f"    -> {n} audio files, sanitized would be: {sanitized!r}")
        print()

    # --- Issue 2b: sanitized album variants ---
    print("--- ALBUM FOLDERS THAT ARE SANITIZED VARIANTS OF EACH OTHER ---\n")
    variants = find_sanitized_album_variants()
    if not variants:
        print("  None found.")
    else:
        for group in variants:
            print(f"  Under: {group[0].parent.name}")
            for d in group:
                n = count_audio(d)
                print(f"    {d.name!r:50s}  {n:3d} audio files")
            print()

    print("=" * 80)
    print("END OF REPORT")
    print("=" * 80)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

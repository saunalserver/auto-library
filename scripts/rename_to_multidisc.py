#!/usr/bin/env python3
"""Rename audio files in multi-disc albums to include disc prefix.

After updating tiddl template to '{disc}-{number:02d} - ...', existing
multi-disc albums still have ambiguous filenames like '01 - Track.flac'
that collide across discs. This script adds the disc prefix to existing
files so the filesystem matches what new downloads will produce.

Single-disc albums are untouched (their files don't collide).

DRY RUN by default. Pass --execute to actually rename.
"""
from __future__ import annotations
import argparse
import re
import sys
from pathlib import Path
from mutagen import File

MUSIC_ROOT = Path("/mnt/photos/flac_music")


def get_disc_number(audio_file: Path) -> int | None:
    """Read disc number from audio metadata. Returns None if missing/invalid."""
    try:
        audio = File(str(audio_file))
        if audio is None:
            return None
        # Try common keys
        for key in ("discnumber", "disc", "DISCNUMBER", "DISC"):
            if key in audio:
                val = audio[key][0]
                if isinstance(val, str):
                    # Sometimes "1/1" format
                    return int(val.split("/")[0])
        return None
    except Exception:
        return None


def get_track_number(audio_file: Path) -> int | None:
    """Read track number from metadata; fall back to parsing filename."""
    try:
        audio = File(str(audio_file))
        if audio is not None:
            for key in ("tracknumber", "track", "TRACKNUMBER", "TRACK"):
                if key in audio:
                    val = audio[key][0]
                    if isinstance(val, str):
                        return int(val.split("/")[0])
    except Exception:
        pass
    # Fallback: parse from filename
    m = re.match(r"^(\d+)", audio_file.name)
    return int(m.group(1)) if m else None


def find_multi_disc_albums() -> list[Path]:
    """Find album folders containing tracks with >1 distinct disc number."""
    out = []
    for artist_dir in MUSIC_ROOT.iterdir():
        if not artist_dir.is_dir():
            continue
        for album_dir in artist_dir.iterdir():
            if not album_dir.is_dir():
                continue
            discs = set()
            for f in list(album_dir.glob("*.flac")) + list(album_dir.glob("*.m4a")):
                d = get_disc_number(f)
                if d is not None:
                    discs.add(d)
            if len(discs) > 1:
                out.append(album_dir)
    return sorted(out)


def rename_album(album_dir: Path, execute: bool) -> tuple[int, int]:
    """Rename files in album_dir to '{disc}-{number:02d} - {rest}'.

    Audio files (.flac, .m4a): renamed based on disc tag.
    .lrc files: matched to their sibling audio by either old-format or
    new-format stem, then renamed to match the audio's canonical stem.

    Idempotent: re-running on already-renamed albums is a no-op.
    """
    renamed, skipped = 0, 0

    audio_files = list(album_dir.glob("*.flac")) + list(album_dir.glob("*.m4a"))

    # Build a map: "audio canonical stem" -> Path (after potential rename)
    # And: "old stem" -> "new stem" so we can pair stray .lrc files
    audio_targets: dict[Path, Path] = {}  # current_path -> target_path
    pair_map: dict[str, str] = {}  # any-known-stem -> canonical new stem

    for f in sorted(audio_files):
        disc = get_disc_number(f)
        if disc is None:
            skipped += 1
            continue
        # What is the canonical new stem for this file?
        # If already in new format "{disc}-{N} - rest", use as-is
        m_new = re.match(r"^(\d+)-(\d+) - (.*)$", f.stem)
        if m_new:
            canonical_stem = f.stem
            old_format_stem = f"{int(m_new.group(2)):02d} - {m_new.group(3)}"
            pair_map[old_format_stem] = canonical_stem
            pair_map[canonical_stem] = canonical_stem
            continue
        # Old format "{N} - rest" → needs rename
        m_old = re.match(r"^(\d+) - (.*)$", f.stem)
        if not m_old:
            print(f"    SKIP (no leading track number): {f.name}")
            skipped += 1
            continue
        canonical_stem = f"{disc}-{m_old.group(1).zfill(2)} - {m_old.group(2)}"
        if canonical_stem == f.stem:
            skipped += 1
            continue
        target = f.parent / (canonical_stem + f.suffix)
        if target.exists():
            print(f"    SKIP (target exists): {f.name} -> {target.name}")
            skipped += 1
            continue
        audio_targets[f] = target
        pair_map[f.stem] = canonical_stem  # old stem → canonical

    # Execute audio renames
    for src, target in audio_targets.items():
        if execute:
            src.rename(target)
        print(f"    {src.name}  ->  {target.name}")
        renamed += 1

    # Pair .lrc files
    for lrc in sorted(album_dir.glob("*.lrc")):
        canonical = pair_map.get(lrc.stem)
        if canonical is None or canonical == lrc.stem:
            skipped += 1
            continue
        new_name = canonical + ".lrc"
        target = lrc.parent / new_name
        if target.exists():
            print(f"    SKIP (lrc target exists): {lrc.name} -> {new_name}")
            skipped += 1
            continue
        if execute:
            lrc.rename(target)
        print(f"    {lrc.name}  ->  {new_name}")
        renamed += 1

    return renamed, skipped


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--execute", action="store_true",
                   help="Actually rename files. Default is dry-run.")
    args = p.parse_args()

    if not MUSIC_ROOT.exists():
        print(f"ERROR: MUSIC_ROOT does not exist: {MUSIC_ROOT}", file=sys.stderr)
        return 1

    albums = find_multi_disc_albums()
    if not albums:
        print("No multi-disc albums found. Nothing to rename.")
        return 0

    print("=" * 80)
    print(f"{'DRY RUN' if not args.execute else 'EXECUTE'} — {len(albums)} multi-disc album(s)")
    print("=" * 80)

    total_renamed, total_skipped = 0, 0
    for album_dir in albums:
        print(f"\n{album_dir.parent.name}/{album_dir.name}")
        renamed, skipped = rename_album(album_dir, args.execute)
        print(f"  summary: {renamed} renamed, {skipped} skipped")
        total_renamed += renamed
        total_skipped += skipped

    print()
    if not args.execute:
        print(f"Dry run: would rename {total_renamed} files. Re-run with --execute.")
    else:
        print(f"Done. Renamed {total_renamed} files, skipped {total_skipped}.")
        print("Trigger Navidrome rescan to refresh index.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

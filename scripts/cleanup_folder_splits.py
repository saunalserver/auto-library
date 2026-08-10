#!/usr/bin/env python3
"""Cleanup music library folder splits (Issues 2 + 3 from tidal audit).

Three kinds of fixes:
  MERGE_ARTIST: artist folders that differ only by case (e.g. 'Charli xcx' vs
    'Charli XCX'). Picks the variant with more albums as canonical, moves
    everything else into it, removes the empty loser.
  MERGE_ALBUM_VARIANTS: under the same artist, album folders that sanitize
    to the same name (e.g. 'RUIN: It's...' alongside 'RUIN Its...'). Picks
    the sanitized form (matches what tiddl produces going forward) and
    moves audio files into it. Files already present at destination are
    kept; only missing files are moved.
  RENAME_ALBUM: a single album folder with unsanitized chars and no
    sanitized counterpart yet. Rename in place.

DRY RUN by default. Pass --execute to actually perform moves.

Navidrome note: moving files will cause Navidrome to re-import them at the
new path on next scan. Play counts keyed to file path may be lost; counts
keyed to metadata (artist+album+title) will survive. Trigger a rescan after.
"""
from __future__ import annotations
import argparse
import re
import shutil
import sys
from collections import defaultdict
from pathlib import Path

MUSIC_ROOT = Path("/mnt/photos/flac_music")
SANITIZE_PATTERN = re.compile(r'[\\/:"*?<>|]+')


def sanitize(name: str) -> str:
    return SANITIZE_PATTERN.sub("", name)


def count_audio(folder: Path) -> int:
    if not folder.is_dir():
        return 0
    return sum(1 for _ in folder.glob("*.flac")) + sum(1 for _ in folder.glob("*.m4a"))


def audio_files(folder: Path) -> list[Path]:
    if not folder.is_dir():
        return []
    return list(folder.glob("*.flac")) + list(folder.glob("*.m4a"))


def lrc_files(folder: Path) -> list[Path]:
    if not folder.is_dir():
        return []
    return list(folder.glob("*.lrc"))


def plan() -> list[dict]:
    """Build the list of operations. Each op is a dict with kind/src/dst/etc."""
    ops = []

    # --- MERGE_ARTIST: group artist dirs by lowercase name ---
    artist_groups: dict[str, list[Path]] = defaultdict(list)
    for d in MUSIC_ROOT.iterdir():
        if d.is_dir() and not d.name.startswith("."):
            artist_groups[d.name.lower().strip()].append(d)

    for key, group in artist_groups.items():
        if len(group) < 2:
            continue
        # Canonical = the one with the most album subfolders (tie-break: most audio)
        group_sorted = sorted(group, key=lambda d: (-(sum(1 for _ in d.iterdir() if _.is_dir())), -count_audio(d)))
        canonical = group_sorted[0]
        others = group_sorted[1:]
        for other in others:
            ops.append({"kind": "MERGE_ARTIST", "canonical": canonical, "src": other})

    # --- MERGE_ALBUM_VARIANTS + RENAME_ALBUM: per-artist album folder fixes ---
    for artist_dir in MUSIC_ROOT.iterdir():
        if not artist_dir.is_dir():
            continue
        # Group album folders by sanitized name
        album_groups: dict[str, list[Path]] = defaultdict(list)
        for album_dir in artist_dir.iterdir():
            if not album_dir.is_dir():
                continue
            album_groups[sanitize(album_dir.name).strip()].append(album_dir)

        for sanitized_key, group in album_groups.items():
            if len(group) > 1:
                # MERGE_ALBUM_VARIANTS: pick the one whose name is already sanitized
                # as canonical (matches future tiddl downloads).
                canonical = next((d for d in group if SANITIZE_PATTERN.search(d.name) is None), None)
                if canonical is None:
                    # No sanitized variant exists — pick the one with most audio
                    canonical = max(group, key=count_audio)
                for src in group:
                    if src is canonical:
                        continue
                    ops.append({"kind": "MERGE_ALBUM_VARIANTS", "canonical": canonical, "src": src})
            elif SANITIZE_PATTERN.search(group[0].name):
                # Single folder with special chars, no sanitized sibling — rename in place
                album_dir = group[0]
                target = album_dir.parent / sanitized_key
                if target.exists() and target != album_dir:
                    # Target name already taken by something else (shouldn't happen
                    # given the grouping above, but guard anyway)
                    continue
                if not target.name or target.name == ".":
                    continue
                ops.append({"kind": "RENAME_ALBUM", "src": album_dir, "dst": target})

    return ops


def describe_op(op: dict) -> str:
    k = op["kind"]
    if k == "MERGE_ARTIST":
        return f"MERGE_ARTIST: move all subfolders from {op['src']!s} -> {op['canonical']!s}"
    if k == "MERGE_ALBUM_VARIANTS":
        src_audio = count_audio(op["src"])
        dst_audio = count_audio(op["canonical"])
        src_lrc = len(lrc_files(op["src"]))
        dst_lrc = len(lrc_files(op["canonical"]))
        return (f"MERGE_ALBUM: {op['src'].parent.name}/{op['src'].name}\n"
                f"         -> {op['canonical'].parent.name}/{op['canonical'].name}\n"
                f"         src: {src_audio} audio, {src_lrc} lrc\n"
                f"         dst: {dst_audio} audio, {dst_lrc} lrc")
    if k == "RENAME_ALBUM":
        return f"RENAME_ALBUM: {op['src'].parent.name}/{op['src'].name} -> {op['dst'].name}"
    return f"UNKNOWN: {op}"


def execute_merge_album(src: Path, canonical: Path) -> tuple[int, int]:
    """Move every file from src into canonical. Skip files that exist at dst.
    Returns (moved, skipped)."""
    moved, skipped = 0, 0
    for f in src.iterdir():
        if not f.is_file():
            continue
        target = canonical / f.name
        if target.exists():
            skipped += 1
            continue
        shutil.move(str(f), str(target))
        moved += 1
    return moved, skipped


def execute_merge_artist(src: Path, canonical: Path) -> tuple[int, int]:
    """For each subfolder of src, either move it wholesale into canonical
    (if no name conflict) or, on conflict, recursively merge files
    (move any file from src subfolder that doesn't exist in dst subfolder).
    Returns (moved_files, skipped_files)."""
    moved, skipped = 0, 0
    for d in src.iterdir():
        if not d.is_dir():
            continue
        target = canonical / d.name
        if not target.exists():
            shutil.move(str(d), str(target))
            # Count files moved for reporting
            moved += sum(1 for _ in target.iterdir() if _.is_file())
            continue
        # Name conflict — merge file-by-file
        m, s = execute_merge_album(d, target)
        moved += m
        skipped += s
        # If src subfolder is now empty, remove it
        if d.exists() and not any(d.iterdir()):
            d.rmdir()
    return moved, skipped


def is_fully_redundant(src: Path, dst: Path) -> bool:
    """True if every file in src exists in dst with identical size.
    Used to decide whether src can be safely deleted."""
    src_files = [f for f in src.iterdir() if f.is_file()]
    if not src_files:
        return False  # empty or only subdirs — don't auto-delete
    for f in src_files:
        dst_file = dst / f.name
        if not dst_file.exists():
            return False
        if f.stat().st_size != dst_file.stat().st_size:
            return False
    return True


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--execute", action="store_true",
                   help="Actually perform the moves. Default is dry-run.")
    args = p.parse_args()

    if not MUSIC_ROOT.exists():
        print(f"ERROR: MUSIC_ROOT does not exist: {MUSIC_ROOT}", file=sys.stderr)
        return 1

    ops = plan()
    if not ops:
        print("No folder splits found. Library is clean.")
        return 0

    print("=" * 80)
    print(f"{'DRY RUN' if not args.execute else 'EXECUTE'} — {len(ops)} operations planned")
    print("=" * 80)
    for op in ops:
        print(describe_op(op))
    print()

    if not args.execute:
        print("Dry run only. Re-run with --execute to perform these changes.")
        print("Recommend: tar czf /tmp/music_backup_$(date +%s).tar.gz <affected paths> first.")
        return 0

    # Actually execute
    print("Executing...")
    total_moved = 0
    total_skipped = 0
    empty_dirs_to_remove = []
    redundant_dirs_to_remove = []
    for op in ops:
        k = op["kind"]
        if k == "MERGE_ALBUM_VARIANTS":
            moved, skipped = execute_merge_album(op["src"], op["canonical"])
            print(f"  moved {moved}, skipped {skipped} (existing): {op['src'].name} -> {op['canonical'].name}")
            total_moved += moved
            total_skipped += skipped
            if op["src"].exists() and is_fully_redundant(op["src"], op["canonical"]):
                redundant_dirs_to_remove.append(op["src"])
            elif op["src"].exists() and not any(op["src"].iterdir()):
                empty_dirs_to_remove.append(op["src"])
        elif k == "MERGE_ARTIST":
            moved, skipped = execute_merge_artist(op["src"], op["canonical"])
            print(f"  moved {moved} files, skipped {skipped}: {op['src'].name} -> {op['canonical'].name}")
            total_moved += moved
            total_skipped += skipped
            # After merge, check if artist folder can be removed
            if op["src"].exists() and not any(op["src"].iterdir()):
                empty_dirs_to_remove.append(op["src"])
        elif k == "RENAME_ALBUM":
            op["src"].rename(op["dst"])
            print(f"  renamed: {op['src'].name} -> {op['dst'].name}")
            total_moved += 1

    # Remove empty source dirs
    for d in empty_dirs_to_remove:
        try:
            d.rmdir()
            print(f"  removed empty: {d}")
        except OSError as e:
            print(f"  WARN: could not remove {d}: {e}", file=sys.stderr)

    # Remove redundant source dirs (verified: all files exist identically in dst)
    for d in redundant_dirs_to_remove:
        # Re-verify before deleting — defensive
        canonical_parent = d.parent
        # Find the matching canonical sibling
        sanitized_name = sanitize(d.name).strip()
        canonical = canonical_parent / sanitized_name
        if canonical != d and canonical.exists() and is_fully_redundant(d, canonical):
            shutil.rmtree(str(d))
            print(f"  removed redundant (all files exist in canonical): {d}")
        else:
            print(f"  kept (no longer redundant or canonical gone): {d}", file=sys.stderr)

    print(f"\nDone. Moved {total_moved}, skipped {total_skipped}, "
          f"removed {len(empty_dirs_to_remove)} empty + {len(redundant_dirs_to_remove)} redundant dirs.")
    print("Trigger Navidrome rescan to update its index.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

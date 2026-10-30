#!/usr/bin/env python3
"""Replace AAC-in-FLAC tracks with real FLAC from Tidal (one-off backfill, safe to re-run).

Until 2026-10-24 tiddl got AAC 320 from Tidal for every track without a
hi-res master (see tiddl_v2.py), saved it as .m4a, and flac-tagger's nightly
m4a_to_flac.sh re-encoded those into .flac: lossy audio, lossless file name.
They are recognisable by the tags tiddl writes on MP4 files, which ffmpeg
copied over: `date=YYYY-MM-DD HH:MM:SS+00:00` and no ISRC.

For each album folder holding such files: download the album again through
tiddl_v2.py into <library drive>/music-staging, check every file decodes and
nothing is missing, then move the old folder to the music trash and the new
one into its place (lyrics sidecars carried over). Tracks Tidal really only
has as AAC come down as .m4a again and are left for the nightly converter.

    python3 scripts/upgrade_aac.py --list            # what would be re-downloaded
    python3 scripts/upgrade_aac.py                   # do it
    python3 scripts/upgrade_aac.py "Artist/Album" …  # just these folders
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import re
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import discovery_picks  # noqa: E402
import library_cleanup as lc  # noqa: E402
import musiclib as m  # noqa: E402
import smart_download  # noqa: E402

STAGING = m.LIBRARY_DRIVE / "music-staging"
REPORT = Path(__file__).resolve().parent.parent / "logs" / "upgrade_aac.json"
MP4_DATE = re.compile(r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d")
log = m.setup_logger("upgrade-aac", "upgrade_aac.log")


def is_aac_transcode(path: Path) -> bool:
    from mutagen.flac import FLAC
    try:
        a = FLAC(path)
    except Exception:  # noqa: BLE001
        return False
    return (any(MP4_DATE.match(v) for v in a.get("date", [])) and not a.get("isrc")) \
        or any(v.startswith("Tidal AAC") for v in a.get("source", []))   # tagged by tiddl_v2.py


def affected_folders() -> list[Path]:
    flacs = [f for f in m.MUSIC_ROOT.rglob("*.flac")]
    with ThreadPoolExecutor(8) as ex:
        hits = [f for f, bad in zip(flacs, ex.map(is_aac_transcode, flacs)) if bad]
    return sorted({f.parent for f in hits})


def album_tag(folder: Path) -> str:
    from mutagen.flac import FLAC
    for f in sorted(folder.glob("*.flac")):
        try:
            album = (FLAC(f).get("album") or [""])[0]
        except Exception:  # noqa: BLE001
            continue
        if album:
            return album
    return folder.name


def squash(s: str) -> str:
    return re.sub(r"[^\w]+", "", s).lower()


def real_flac_count(files) -> int:
    return sum(1 for f in files if f.suffix.lower() == ".flac" and not is_aac_transcode(f))


def upgrade(folder: Path, album_id: str | None = None) -> dict:
    rel = str(folder.relative_to(m.MUSIC_ROOT))
    artist_dir = folder.relative_to(m.MUSIC_ROOT).parts[0]   # a "/" in a title nests folders
    old = m.audio_files(folder)
    result = {"folder": rel, "before": len(old), "aac_before": sum(map(is_aac_transcode, old))}
    if not album_id:
        with contextlib.redirect_stdout(io.StringIO()):
            match = smart_download.find_best_album_match(artist_dir, album_tag(folder), min_tracks=1)
        if not match or not match[0]:
            return {**result, "status": "not found on Tidal"}
        album_id, title, artist, count = match
        result.update(tidal=f"{artist} - {title}", tidal_tracks=count)
    result["album_id"] = album_id

    stage = STAGING / str(album_id)
    shutil.rmtree(stage, ignore_errors=True)
    stage.mkdir(parents=True)
    m.ensure_tidal_token(log)
    with discovery_picks.download_lock(log):
        r = subprocess.run([*m.TIDDL_DOWNLOAD, "url", f"album/{album_id}", "download",
                            "-p", str(stage), "--scan-path", str(stage)],
                           capture_output=True, text=True, timeout=3600)
    dirs = sorted({f.parent for f in stage.rglob("*") if f.suffix.lower() in (".flac", ".m4a")})
    if r.returncode != 0 or len(dirs) != 1:
        return {**result, "status": f"download failed (exit {r.returncode}, {len(dirs)} folders)"}
    new_dir = dirs[0]
    if squash(str(new_dir.relative_to(stage))) != squash(rel):
        return {**result, "status": f"Tidal folder differs: {new_dir.relative_to(stage)}"}
    new = m.audio_files(new_dir)
    broken = [f.name for f in new if f.suffix.lower() == ".flac" and lc.is_broken(f)]
    result.update(after=len(new), real_flac_before=real_flac_count(old), real_flac_after=real_flac_count(new))
    if broken:
        return {**result, "status": f"{len(broken)} new file(s) do not decode"}
    if len(new) < len(old):
        return {**result, "status": "fewer tracks than before, kept the old folder"}
    if result["real_flac_after"] <= result["real_flac_before"]:
        shutil.rmtree(stage, ignore_errors=True)
        return {**result, "status": "Tidal still only has AAC, kept the old folder"}

    for lrc in folder.glob("*.lrc"):                     # carry lyrics over by (number, title)
        mo = lc._NUMBERED.match(lrc.stem)
        if not mo:
            continue
        key = (int(mo.group(2)), mo.group(3).split(" - ")[-1].strip().lower())
        for f in new:
            fm = lc._NUMBERED.match(f.stem)
            if fm and (int(fm.group(2)), fm.group(3).split(" - ")[-1].strip().lower()) == key \
                    and not f.with_suffix(".lrc").exists():
                shutil.copy2(lrc, f.with_suffix(".lrc"))
    lc.trash_folder(folder, "AAC transcodes replaced by real FLAC from Tidal", log)
    folder.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(new_dir), str(folder))
    shutil.rmtree(stage, ignore_errors=True)
    lc.unify_album_tags(folder, log)
    return {**result, "status": "upgraded"}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("folders", nargs="*", help="Artist/Album paths relative to the library, "
                    "optionally =<Tidal album id> when the search picks the wrong release")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--pause", type=int, default=10, help="seconds between albums (Tidal rate limits)")
    args = ap.parse_args()
    if not m.ensure_library(log, "upgrade-aac"):
        return 1
    ids = dict(f.rsplit("=", 1) if "=" in f else (f, None) for f in args.folders)
    folders = [m.MUSIC_ROOT / f for f in ids] if ids else affected_folders()
    if args.list:
        for f in folders:
            print(f.relative_to(m.MUSIC_ROOT))
        print(f"{len(folders)} folder(s)")
        return 0
    results = []
    for i, folder in enumerate(folders, 1):
        try:
            res = upgrade(folder, ids.get(str(folder.relative_to(m.MUSIC_ROOT))) if ids else None)
        except Exception as exc:  # noqa: BLE001
            res = {"folder": str(folder.relative_to(m.MUSIC_ROOT)), "status": f"error: {exc}"}
        results.append(res)
        log.info("[%d/%d] %s: %s", i, len(folders), res["folder"], res["status"])
        REPORT.write_text(json.dumps(results, indent=1, ensure_ascii=False))
        time.sleep(args.pause)
    done = [r for r in results if r["status"] == "upgraded"]
    log.info("Upgraded %d of %d folders; report in %s", len(done), len(results), REPORT)
    if done:
        try:
            m.Subsonic(client="upgrade-aac").rescan(log)
        except Exception as exc:  # noqa: BLE001
            log.warning("rescan: %s", exc)
    return 0


if __name__ == "__main__":
    sys.exit(main())

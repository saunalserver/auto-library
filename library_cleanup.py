#!/usr/bin/env python3
"""Folder-level library cleanup: retire singles an album has made redundant, trash/restore folders.

A single you got from Pitchfork (or a scrobble) stays in its own folder when
the album watch later downloads the full album — 29 such pairs were found on
2026-09-26 (AZ Chike's "Packed Up" next to "No Rest for The Wicked", ...).
After every album download the downloaders now call
`retire_superseded_singles`, which moves a sibling single/EP folder to the
trash when *every* one of its tracks is audio-identical (chromaprint, the
same fingerprints the weekly dedup scan uses) to a track on the new album.
Titles alone are not trusted: a remix single shares its title with the album
version.

Trash is /mnt/photos/music-trash/<date>/<path relative to the library> — on
the music drive itself, so a move is an instant rename and cannot fill the
system disk. Nothing is ever deleted here.

    python3 library_cleanup.py singles            # report existing redundant singles
    python3 library_cleanup.py singles --apply    # ...and move them to the trash
    python3 library_cleanup.py trash "<folder>" --reason "..."
    python3 library_cleanup.py restore "<folder>"  # path as it was in the library
    python3 library_cleanup.py list-trash
    python3 library_cleanup.py repair-playlists   # after a rescan
    python3 library_cleanup.py verify [--changed] # corrupt files -> trash -> re-download
"""
from __future__ import annotations

import argparse
import logging
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import musiclib as m  # noqa: E402

TRASH_ROOT = m.LIBRARY_MOUNT / "music-trash"
MAX_SINGLE_TRACKS = 3
MATCH = 0.95

_log = logging.getLogger("library-cleanup")


# --- trash ---------------------------------------------------------------------------
def _log_action(path: Path, action: str, details: str) -> None:
    conn = m.db_connect()
    conn.execute("CREATE TABLE IF NOT EXISTS dedup_log (id INTEGER PRIMARY KEY AUTOINCREMENT, filepath TEXT NOT NULL,"
                 " action TEXT NOT NULL, when_at TIMESTAMP NOT NULL, actor TEXT, details TEXT)")
    conn.execute("INSERT INTO dedup_log (filepath, action, when_at, actor, details) "
                 "VALUES (?, ?, datetime('now'), 'library_cleanup', ?)", (str(path), action, details))
    conn.commit()
    conn.close()


def trash_folder(folder: Path, reason: str, logger: logging.Logger = _log) -> Path:
    """Move a library folder into the trash, keeping its path. Returns where it went."""
    folder = Path(folder).resolve()
    rel = folder.relative_to(m.MUSIC_ROOT.resolve())
    dest = TRASH_ROOT / datetime.now().strftime("%Y-%m-%d") / rel
    if dest.exists():
        dest = dest.with_name(f"{dest.name} ({datetime.now():%H%M%S})")
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(folder), str(dest))
    _log_action(folder, "trash-folder", f"{dest} | {reason}")
    logger.info("Trashed %s → %s (%s)", rel, dest, reason)
    parent = folder.parent
    if parent != m.MUSIC_ROOT.resolve() and parent.is_dir() and not any(parent.iterdir()):
        parent.rmdir()                                   # last album of that artist
    return dest


def restore_folder(original: str) -> Path:
    rel = Path(original).resolve().relative_to(m.MUSIC_ROOT.resolve()) if original.startswith("/") else Path(original)
    hits = sorted(TRASH_ROOT.glob(f"*/{rel}"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not hits:
        raise FileNotFoundError(f"not in {TRASH_ROOT}: {rel}")
    target = m.MUSIC_ROOT / rel
    if target.exists():
        raise FileExistsError(f"already exists in the library: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(hits[0]), str(target))
    _log_action(target, "restore-folder", str(hits[0]))
    return target


# --- corrupt downloads ---------------------------------------------------------------
def is_broken(path: Path) -> bool:
    """True if the file does not fully decode.

    Seen 2026-09: tiddl exited 0 leaving FLACs whose header says 4 minutes but
    whose audio frames do not decode (Kelela "point blank" and others) — they
    play as silence or skip, and fpcalc reports "Not enough audio data".
    """
    import subprocess
    path = Path(path)
    if path.suffix.lower() == ".flac":
        return subprocess.run(["flac", "-t", "-s", str(path)], capture_output=True).returncode != 0
    r = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-map", "0:a:0", "-f", "null", "-"],
                       capture_output=True, text=True)
    return r.returncode != 0 or bool(r.stderr.strip())


def remove_broken_files(album_dirs, logger: logging.Logger = _log) -> list[str]:
    """Move files that don't decode (and their lyrics) to the trash so a re-download replaces them."""
    broken = []
    for d in album_dirs:
        for f in m.audio_files(Path(d)):
            if not is_broken(f):
                continue
            for p in (f, f.with_suffix(".lrc")):
                if p.exists():
                    dest = TRASH_ROOT / datetime.now().strftime("%Y-%m-%d") / p.relative_to(m.MUSIC_ROOT)
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(p), str(dest))
                    _log_action(p, "trash-file", f"{dest} | does not decode (corrupt download)")
            logger.warning("Corrupt file removed for re-download: %s", f.relative_to(m.MUSIC_ROOT))
            broken.append(f.name)
    return broken


def verify(logger: logging.Logger = _log, changed_only: bool = False, workers: int = 4) -> tuple[list[str], list[str]]:
    """Find files that don't decode, move them out and re-download their albums from Tidal.

    changed_only: only files modified since the previous verify (the weekly run).
    Returns (repaired albums, albums still broken).
    """
    import subprocess
    import time
    from concurrent.futures import ThreadPoolExecutor
    conn = m.db_connect()
    since = float(m.get_state(conn, "last_verify") or 0) - 86400 if changed_only else 0
    conn.close()
    started = time.time()
    files = [p for p in m.MUSIC_ROOT.rglob("*") if p.suffix.lower() in m.AUDIO_EXTS and p.stat().st_mtime > since]
    logger.info("Verifying %d audio files%s", len(files), " (changed since last check)" if changed_only else "")
    with ThreadPoolExecutor(workers) as pool:
        bad = [f for f, broken in zip(files, pool.map(is_broken, files)) if broken]
    repaired, still_broken = repair_albums(sorted({f.parent for f in bad}), logger)
    conn = m.db_connect()
    m.set_state(conn, "last_verify", str(int(started)))
    conn.close()
    return repaired, still_broken


def repair_albums(albums, logger: logging.Logger = _log) -> tuple[list[str], list[str]]:
    """Move out the broken files of each album folder and re-download them from Tidal."""
    import subprocess
    import discovery_picks
    repaired, still_broken = [], []
    for album in albums:
        label = f"{album.parent.name} - {album.name}"
        before = len(m.audio_files(album))
        removed = remove_broken_files([album], logger)
        logger.info("Re-downloading %s (%d corrupt file(s))", label, len(removed))
        with discovery_picks.download_lock(logger):
            r = subprocess.run(["python3", str(Path(__file__).resolve().parent / "smart_download.py"),
                                album.parent.name, album.name, "1"], capture_output=True, text=True, timeout=1800)
        dedupe_renumbered(album, logger)
        still_bad = remove_broken_files([album], logger)
        after = len(m.audio_files(album))
        if r.returncode == 0 and not still_bad and after >= before:
            repaired.append(label)
        else:
            logger.error("Could not repair %s: exit %s, %d/%d tracks, %d still corrupt",
                         label, r.returncode, after, before, len(still_bad))
            still_broken.append(f"{label} ({after}/{before} tracks)")
    return repaired, still_broken


# --- one track, two file names ------------------------------------------------------------
_NUMBERED = re.compile(r"^(?:(\d+)-)?(\d+) - (.+)$")


def dedupe_renumbered(album_dir: Path, logger: logging.Logger = _log) -> list[str]:
    """Collapse '02 - X.flac' + '1-02 - X.flac' (the same track under old and new tiddl naming).

    tiddl's template now writes '<disc>-<nn> - <artist> - <title>'; older
    downloads are '<nn> - …'. tiddl skips a track only if the *new* name exists,
    so every re-download of an old album added a full second copy
    (Wallsocket and SISTER had every track twice). Keeps the new-style name —
    the one tiddl will recognise next time — and carries lyrics over.
    """
    groups: dict[tuple, list[Path]] = {}
    for f in m.audio_files(Path(album_dir)):
        mo = _NUMBERED.match(f.stem)
        if mo:
            groups.setdefault((int(mo.group(2)), mo.group(3).split(" - ")[-1].strip().lower()), []).append(f)
    removed = []
    for files in groups.values():
        if len(files) < 2:
            continue
        files.sort(key=lambda f: (_NUMBERED.match(f.stem).group(1) is None, is_broken(f)))
        keep = files[0]
        for f in files[1:]:
            lrc = f.with_suffix(".lrc")
            if lrc.exists() and not keep.with_suffix(".lrc").exists():
                shutil.move(str(lrc), str(keep.with_suffix(".lrc")))
            for p in (f, lrc):
                if p.exists():
                    dest = TRASH_ROOT / datetime.now().strftime("%Y-%m-%d") / p.relative_to(m.MUSIC_ROOT)
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(p), str(dest))
                    _log_action(p, "trash-file", f"{dest} | same track as {keep.name}")
            logger.info("Same track twice, kept %s, trashed %s", keep.name, f.name)
            removed.append(f.name)
    return removed


# --- redundant singles ---------------------------------------------------------------
def _fingerprint(path: Path, cache: dict):
    """Chromaprint for a file: the dedup scan's stored one if current, else computed now."""
    key = str(path)
    if key in cache:
        return cache[key]
    fp = None
    try:
        import dedup_state
        conn = m.db_connect()
        row = conn.execute("SELECT * FROM audio_fingerprints WHERE filepath = ?", (key,)).fetchone()
        conn.close()
        if row and abs((row["file_mtime"] or 0) - path.stat().st_mtime) < 1.0:
            fp = dedup_state._row_to_fp(row)
    except Exception:  # noqa: BLE001 — no stored fingerprint: compute one below
        pass
    if fp is None:
        try:
            from dedup_lib import fingerprint_file
            fp = fingerprint_file(path)
        except Exception as exc:  # noqa: BLE001 — fpcalc fails on clips < ~3 s
            _log.warning("No fingerprint for %s: %s", path, exc)
    cache[key] = fp
    return fp


def contained_in(single: Path, album: Path, cache: Optional[dict] = None) -> bool:
    """True if every track in `single` is audio-identical to some track in `album`."""
    from dedup_lib import compare_fingerprints
    cache = {} if cache is None else cache
    single_files, album_files = m.audio_files(single), m.audio_files(album)
    if not single_files or len(single_files) >= len(album_files):
        return False
    album_fps = [fp for fp in (_fingerprint(f, cache) for f in album_files) if fp]
    for f in single_files:
        fp = _fingerprint(f, cache)
        if fp is None or not any(compare_fingerprints(fp, a) >= MATCH for a in album_fps):
            return False
    return True


def superseded_singles(album: Path, cache: Optional[dict] = None) -> list[Path]:
    """Sibling single/EP folders (≤3 tracks, same artist folder) that `album` fully contains."""
    album = Path(album)
    cache = {} if cache is None else cache
    out = []
    for sibling in sorted(album.parent.iterdir()):
        if sibling == album or not sibling.is_dir():
            continue
        n = len(m.audio_files(sibling))
        if 0 < n <= MAX_SINGLE_TRACKS and contained_in(sibling, album, cache):
            out.append(sibling)
    return out


def retire_superseded_singles(album_dirs, logger: logging.Logger = _log) -> list[str]:
    """Trash the singles a freshly downloaded album makes redundant. Never raises."""
    retired = []
    for album in album_dirs:
        try:
            for single in superseded_singles(Path(album)):
                trash_folder(single, f"all tracks are on '{Path(album).name}'", logger)
                retired.append(f"{single.parent.name} - {single.name}")
        except Exception as exc:  # noqa: BLE001 — cleanup must never fail a download
            logger.warning("Single cleanup failed for %s: %s", album, exc)
    return retired


def repair_playlists(logger: logging.Logger = _log) -> int:
    """Point playlist entries whose file is gone at the same track elsewhere in the library.

    Navidrome playlists hold file IDs, so retiring a single emptied its slot
    in every Pitchfork Selects playlist it was on. Needs a finished rescan
    first (the album's copy must be indexed). Keeps playlist order; entries
    with no replacement are left as they are. Returns entries repaired.
    """
    artist_names, title_key = m.artist_names, m.title_key
    missing = m.navidrome_sql(
        "SELECT pt.playlist_id, mf.artist, mf.album_artist, mf.title FROM playlist_tracks pt "
        "JOIN media_file mf ON mf.id = pt.media_file_id WHERE mf.missing = 1")
    if not missing:
        return 0
    present: dict[str, list[tuple[set, str]]] = {}
    for r in m.navidrome_sql("SELECT id, artist, album_artist, title FROM media_file WHERE missing = 0"):
        present.setdefault(title_key(r["title"]), []).append(
            (artist_names(r["artist"]) | artist_names(r["album_artist"]), r["id"]))

    def replacement(r) -> Optional[str]:
        names = artist_names(r["artist"]) | artist_names(r["album_artist"])
        return next((sid for lib_names, sid in present.get(title_key(r["title"]), []) if lib_names & names), None)

    sub = m.Subsonic(client="library-cleanup")
    repaired = 0
    for playlist_id in sorted({r["playlist_id"] for r in missing}):
        rows = m.navidrome_sql(
            "SELECT mf.id, mf.missing, mf.artist, mf.album_artist, mf.title FROM playlist_tracks pt "
            f"JOIN media_file mf ON mf.id = pt.media_file_id WHERE pt.playlist_id = '{playlist_id}' ORDER BY pt.id")
        ids, fixed = [], 0
        for r in rows:
            new = replacement(r) if r["missing"] else None
            ids.append(new or r["id"])
            fixed += bool(new)
        if fixed:
            sub.call("createPlaylist", {"playlistId": playlist_id, "songId": ids})   # replaces the track list
            logger.info("Playlist %s: re-pointed %d track(s) to the album copies", playlist_id, fixed)
            repaired += fixed
    return repaired


def find_all_superseded(logger: logging.Logger = _log) -> list[tuple[Path, Path]]:
    """(single, album) for every redundant single in the library."""
    pairs = []
    cache: dict = {}
    for artist in sorted(p for p in m.MUSIC_ROOT.iterdir() if p.is_dir()):
        albums = [d for d in artist.iterdir() if d.is_dir()]
        singles = [d for d in albums if 0 < len(m.audio_files(d)) <= MAX_SINGLE_TRACKS]
        for single in singles:
            for album in sorted(albums, key=lambda d: -len(m.audio_files(d))):
                if album != single and contained_in(single, album, cache):
                    pairs.append((single, album))
                    break
    return pairs


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("singles", help="find singles fully contained in an album by the same artist")
    p.add_argument("--apply", action="store_true", help="move them to the trash")
    p = sub.add_parser("trash", help="move a library folder to the trash")
    p.add_argument("folder")
    p.add_argument("--reason", default="manual")
    p = sub.add_parser("restore", help="put a trashed folder back")
    p.add_argument("folder", help="the folder's original path (absolute, or relative to the library)")
    sub.add_parser("list-trash")
    sub.add_parser("repair-playlists", help="re-point playlist entries whose file is gone")
    p = sub.add_parser("verify", help="find files that don't decode and re-download their albums")
    p.add_argument("--changed", action="store_true", help="only files changed since the last verify")
    args = ap.parse_args()
    logger = m.setup_logger("library-cleanup", "library_cleanup.log")
    if not m.library_available():
        logger.error("Music drive not mounted")
        return 2

    if args.cmd == "singles":
        pairs = find_all_superseded(logger)
        for single, album in pairs:
            print(f"{single.relative_to(m.MUSIC_ROOT)}  →  on '{album.name}'")
            if args.apply:
                trash_folder(single, f"all tracks are on '{album.name}'", logger)
        print(f"{len(pairs)} redundant single folder(s){' moved to ' + str(TRASH_ROOT) if args.apply else ''}")
    elif args.cmd == "trash":
        print(trash_folder(Path(args.folder), args.reason, logger))
    elif args.cmd == "restore":
        print(restore_folder(args.folder))
    elif args.cmd == "verify":
        m.ensure_tidal_token(logger)
        repaired, broken = verify(logger, changed_only=args.changed)
        print(f"repaired {len(repaired)}, still broken {len(broken)}")
        if repaired or broken:
            lines = ([f"Re-downloaded {len(repaired)} album(s) with corrupt files:", m.fmt_list(repaired, 10, "  + ")]
                     if repaired else []) + ([f"Still broken ({len(broken)}):", m.fmt_list(broken, 10, "  x ")]
                                             if broken else [])
            m.notify("Corrupt music files", "\n".join(lines), "warning,wrench", "high" if broken else "default",
                     logger=logger)
            if repaired:
                m.Subsonic(client="library-cleanup").rescan(logger)
    elif args.cmd == "repair-playlists":
        print(f"{repair_playlists(logger)} playlist entries repaired")
    elif args.cmd == "list-trash":
        for d in sorted(TRASH_ROOT.glob("*/*/*")) if TRASH_ROOT.exists() else []:
            print(d.relative_to(TRASH_ROOT))
    return 0


if __name__ == "__main__":
    sys.exit(main())

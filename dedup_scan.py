"""Scan the music library and fingerprint every audio file.

Idempotent — skips files whose mtime hasn't changed since last scan.
"""
from __future__ import annotations

import argparse
import logging
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from dedup_lib import init_dedup_schema, index_file, fingerprint_file, normalize
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


class LibraryUnavailable(RuntimeError):
    """Raised when the root has no audio files at all (drive not mounted / I/O error)."""


def list_audio_files(root: Path) -> list[Path]:
    root = Path(root)
    return sorted(p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in AUDIO_EXTS)


def prune_missing(conn: sqlite3.Connection, present: set[str]) -> int:
    """Drop fingerprint rows (and their pending findings) for files that no longer exist.

    Only called after the caller has verified the library is readable, so a
    dead drive can never wipe the index.
    """
    rows = conn.execute("SELECT filepath FROM audio_fingerprints").fetchall()
    gone = [r[0] for r in rows if r[0] not in present]
    for fp in gone:
        conn.execute("DELETE FROM audio_fingerprints WHERE filepath = ?", (fp,))
        conn.execute("DELETE FROM dedup_findings WHERE status = 'pending' AND (filepath = ? OR matched_path = ?)", (fp, fp))
    conn.commit()
    return len(gone)


def scan_library(conn: sqlite3.Connection, root: Path, workers: int = 1, prune: bool = False) -> int:
    """Walk root and fingerprint every audio file that is new or changed.

    Returns the number of files fingerprinted this run. Files fpcalc cannot
    handle (e.g. clips shorter than ~3 s) are logged and skipped instead of
    aborting the whole scan. Raises LibraryUnavailable if there are no audio
    files at all, so a missing drive is loud rather than a silent "0 files".
    """
    files = list_audio_files(root)
    if not files:
        raise LibraryUnavailable(f"no audio files under {root} — drive not mounted?")
    if prune:
        n_pruned = prune_missing(conn, {str(p) for p in files})
        if n_pruned:
            log.info("Pruned %d fingerprints for files that no longer exist", n_pruned)

    # Decide which files need (re)fingerprinting: new, or mtime changed.
    known = {r[0]: r[1] for r in conn.execute("SELECT filepath, file_mtime FROM audio_fingerprints")}
    todo = []
    for p in files:
        try:
            mtime = p.stat().st_mtime
        except OSError:
            continue
        prev = known.get(str(p))
        if prev is None or abs(prev - mtime) >= 1.0:
            todo.append(p)
    log.info("%d audio files, %d to fingerprint", len(files), len(todo))

    def work(p: Path):
        try:
            return p, parse_metadata(p), fingerprint_file(p), None
        except Exception as exc:  # noqa: BLE001
            return p, None, None, exc

    count = 0
    errors = 0
    started = time.time()
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        for path, meta, fp, exc in pool.map(work, todo):
            if exc is not None:
                errors += 1
                log.warning("skip %s: %s", path, str(exc)[:120])
                continue
            artist, album, title = meta
            try:
                stat = path.stat()
                conn.execute("""
                    INSERT INTO audio_fingerprints
                        (filepath, artist, album, title, normalized_artist, normalized_title,
                         duration_ms, fingerprint, fingerprint_version, file_size, file_mtime, scanned_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(filepath) DO UPDATE SET
                        artist=excluded.artist, album=excluded.album, title=excluded.title,
                        normalized_artist=excluded.normalized_artist, normalized_title=excluded.normalized_title,
                        duration_ms=excluded.duration_ms, fingerprint=excluded.fingerprint,
                        fingerprint_version=excluded.fingerprint_version, file_size=excluded.file_size,
                        file_mtime=excluded.file_mtime, scanned_at=excluded.scanned_at
                """, (str(path), artist, album, title, normalize(artist), normalize(title),
                      fp.duration_ms, fp.fingerprint_b64, fp.fingerprint_version,
                      stat.st_size, stat.st_mtime, time.time()))
                count += 1
                # Short transactions: other automations share this DB and must
                # not wait behind a long fingerprinting batch.
                if count % 20 == 0:
                    conn.commit()
                if count % 200 == 0:
                    log.info("  %d/%d fingerprinted (%.0fs)", count, len(todo), time.time() - started)
            except Exception as exc:  # noqa: BLE001
                errors += 1
                log.warning("db insert failed for %s: %s", path, exc)
    conn.commit()
    if errors:
        log.warning("%d files could not be fingerprinted", errors)
    return count


def main():
    parser = argparse.ArgumentParser(description="Fingerprint the music library")
    parser.add_argument("--root", default="/mnt/photos/flac_music")
    parser.add_argument("--db", default="database/monitor.db")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    conn = sqlite3.connect(args.db)
    init_dedup_schema(conn)
    n = scan_library(conn, Path(args.root), workers=args.workers, prune=True)
    log.info("Fingerprinted %d files", n)
    conn.close()


if __name__ == "__main__":
    sys.exit(main())

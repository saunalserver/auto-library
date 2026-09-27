"""Discovery suggestions you pick from by replying on ntfy.

Sunday's discovery run no longer downloads anything by itself — it once
fetched five Flo Rida albums and the wrong Mahler symphony in one go. It
resolves each suggestion on Tidal, stores the numbered list here and posts it
to the `music` topic. You reply in that topic with the numbers you want
("1,3,5", "2-4", "all", "none"); `music_replies.py` reads the reply and calls
`download_picks`.
"""
from __future__ import annotations

import contextlib
import fcntl
import io
import json
import logging
import os
import re
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional

import library_cleanup
import musiclib as m

STATE_KEY = "discovery_suggestions"
MAX_AGE = 8 * 86400            # a list is valid until the next Sunday's replaces it
LOCK_PATH = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")) / "auto-library.lock"


@dataclass
class Suggestion:
    n: int
    artist: str          # Tidal's spelling — that is the folder tiddl writes
    title: str
    album_id: str
    tracks: int
    reason: str          # "top artist" / "like <artist>"
    status: str = "open"  # open | downloaded | failed | owned

    def label(self) -> str:
        return f"{self.artist} - {self.title}"


# --- the stored list -------------------------------------------------------------------
def save(suggestions: list[Suggestion], conn=None, created: Optional[float] = None) -> None:
    own = conn is None
    conn = conn or m.db_connect()
    m.set_state(conn, STATE_KEY, json.dumps({"created": created or time.time(),
                                             "items": [asdict(s) for s in suggestions]}))
    if own:
        conn.close()


def load(conn=None) -> tuple[list[Suggestion], float]:
    """(suggestions, created) — ([], 0) if there is no list."""
    own = conn is None
    conn = conn or m.db_connect()
    raw = m.get_state(conn, STATE_KEY)
    if own:
        conn.close()
    if not raw:
        return [], 0.0
    data = json.loads(raw)
    return [Suggestion(**d) for d in data.get("items", [])], float(data.get("created", 0))


# --- reading a reply -------------------------------------------------------------------
_NONE = {"none", "no", "skip", "nothing", "0"}
_ALL = {"all", "everything", "yes"}


def parse_reply(text: str, count: int) -> Optional[list[int]]:
    """Numbers picked in a reply, in order, without duplicates.

    '1,3,5' / '1 3 5' / '#2 and #4' / '2-4' / 'all' -> list;  'none' -> [].
    None means "this is not a pick" (so a stray message is not an order).
    Numbers outside 1..count are dropped.
    """
    words = text.strip().lower()
    if not words:
        return None
    if words in _NONE:
        return []
    if words in _ALL:
        return list(range(1, count + 1))
    # Anything besides numbers, ranges, separators and "and" is not a pick.
    words = re.sub(r"\band\b", " ", words)
    if re.search(r"[^\d\s,;#&\-–.+]", words):
        return None
    picks: list[int] = []
    for a, b in re.findall(r"(\d+)(?:\s*[-–]\s*(\d+))?", words):
        lo, hi = int(a), int(b or a)
        for n in range(min(lo, hi), max(lo, hi) + 1):
            if 1 <= n <= count and n not in picks:
                picks.append(n)
    return picks or None


# --- resolving candidates on Tidal -------------------------------------------------------
def resolve(artist: str, album: str, min_tracks: int = 5) -> tuple[str, Optional[tuple[str, str, str, int]]]:
    """Look an album up on Tidal without downloading it.

    Returns (status, match): status is 'ok', 'owned', 'not_found' or 'api_error';
    match is (album_id, tidal_title, tidal_artist, track_count) for 'ok'/'owned'.
    """
    import smart_download as sd
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            album_id, title, found_artist, tracks = sd.find_best_album_match(artist, album, min_tracks)
    except sd.ApiError:
        return "api_error", None
    # -1 = matched through a track's parent release (singles); not an album pick.
    if not album_id or tracks < min_tracks:
        return "not_found", None
    match = (str(album_id), title, found_artist, tracks)
    if sd.owned_on_disk(found_artist, title, tracks):
        return "owned", match
    return "ok", match


# --- downloading picks -----------------------------------------------------------------------
@contextlib.contextmanager
def download_lock(logger: logging.Logger, wait: int = 1800):
    """The lock the monitor/discovery/pitchfork units take via flock(1), so downloads never overlap."""
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(LOCK_PATH, "a") as fh:
        deadline = time.time() + wait
        while True:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.time() > deadline:
                    raise TimeoutError("another music download has held the lock for 30 min")
                logger.info("Waiting for another download to finish…")
                time.sleep(15)
        try:
            yield
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def record_download(s: Suggestion, file_count: int) -> None:
    conn = m.db_connect()
    conn.execute("INSERT OR IGNORE INTO downloaded_albums (artist, album, download_date, file_count) "
                 "VALUES (?, ?, datetime('now'), ?)", (s.artist, s.title, file_count))
    conn.commit()
    conn.close()


def download_picks(picks: list[Suggestion], logger: logging.Logger) -> tuple[list[str], list[str], list[str]]:
    """Download each picked album by its Tidal ID. Returns (done, failed, retired singles) labels."""
    done, failed, retired = [], [], []
    for s in picks:
        existing = m.count_audio_files(s.artist, s.title)
        if existing and existing >= s.tracks:
            logger.info("Already on disk: %s (%d files)", s.label(), existing)
            s.status = "owned"
            done.append(f"{s.label()} (already had it)")
            continue
        logger.info("Downloading pick %d: %s (album %s)", s.n, s.label(), s.album_id)
        try:
            result = subprocess.run([m.TIDDL_BIN, "url", f"album/{s.album_id}", "download"],
                                    capture_output=True, text=True, timeout=1800)
        except Exception as exc:  # noqa: BLE001
            logger.error("tiddl failed for %s: %s", s.label(), exc)
            s.status = "failed"
            failed.append(s.label())
            continue
        for d in m.find_album_dirs(s.artist, s.title):
            library_cleanup.dedupe_renumbered(d, logger)
        broken = library_cleanup.remove_broken_files(m.find_album_dirs(s.artist, s.title), logger)
        on_disk = m.count_audio_files(s.artist, s.title)
        if broken:
            # Removed; replying with the same number again downloads just those tracks.
            logger.error("Pick %d: %d corrupt file(s) removed", s.n, len(broken))
            s.status = "open"
            failed.append(f"{s.label()} ({len(broken)} corrupt tracks — reply {s.n} again to retry)")
            continue
        if result.returncode != 0 or on_disk == 0:
            # tiddl exits 0 into an unplugged drive; only files on disk count.
            logger.error("Pick %d not on disk after download (exit %s): %s",
                         s.n, result.returncode, (result.stderr or result.stdout)[-300:])
            s.status = "failed"
            failed.append(s.label())
            continue
        logger.info("SUCCESS: %s (%d/%d files)", s.label(), on_disk, s.tracks)
        record_download(s, on_disk)
        s.status = "downloaded"
        done.append(s.label() + ("" if on_disk >= s.tracks else f" ({on_disk}/{s.tracks} tracks)"))
        retired.extend(library_cleanup.retire_superseded_singles(m.find_album_dirs(s.artist, s.title), logger))
    return done, failed, retired

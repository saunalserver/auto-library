#!/usr/bin/env python3
"""
Discovery suggestions — Sunday: albums you might want, as a numbered ntfy list.

Nothing is downloaded here. Until 2026-09 this job downloaded up to 10 albums a
week on its own, and it made a mess: five Flo Rida albums after one week of
listening, the same Berlioz symphony three times under three spellings,
Mahler's Symphony No. 2 for "No. 5", and a 154-track Mendelssohn box. Now it:

  * takes one album per artist (7-day top artists, then artists similar to them
    that are not in the library yet),
  * looks every candidate up on Tidal first and drops it if Tidal's copy is
    already on disk, if it is a single/short release (< 5 tracks), a remix or
    live album, or if you skipped it in last week's list,
  * posts the list to ntfy `music`. Reply in that topic with the numbers you
    want ("1,3,5", "2-4", "all") and `music_replies.py` downloads them.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import discovery_picks as dp  # noqa: E402
import musiclib as m  # noqa: E402

MAX_SUGGESTIONS = 10
FROM_TOP = 5                 # of which albums by artists you already play
SIMILAR_PER_ARTIST = 2       # so one favourite does not fill the whole list
TRIES_PER_ARTIST = 4         # Last.fm albums to try per artist before moving on
MIN_TRACKS = 5
DECLINED_KEY = "discovery_declined"
SKIP_TITLE_RE = re.compile(r"\b(remix(es)?|live|karaoke|instrumentals?|acapellas?|sped up|slowed)\b", re.I)


def norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def top_albums(artist: str, limit: int) -> list[str]:
    data = m.lastfm_call({"method": "artist.gettopalbums", "artist": artist,
                          "limit": str(limit), "autocorrect": "1"})
    items = m._as_list((data or {}).get("topalbums", {}).get("album"))
    return [a["name"] for a in items if a.get("name") and a["name"] != "(null)"]


class Library:
    """What is already here: Navidrome (artist, album) pairs, their artists, download history."""

    def __init__(self, logger):
        self.albums: set[tuple[str, str]] = set()
        self.artists: set[str] = set()
        try:
            for r in m.navidrome_sql("SELECT DISTINCT album_artist, artist, album FROM media_file"):
                for a in {norm(r.get("album_artist")), norm(r.get("artist"))}:
                    self.artists.add(a)
                    self.albums.add((a, norm(r.get("album"))))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Navidrome index unavailable: %s", exc)
        conn = m.db_connect()
        for row in conn.execute("SELECT artist, album FROM downloaded_albums"):
            self.albums.add((norm(row[0]), norm(row[1])))
        conn.close()
        logger.info("Library: %d artists, %d albums", len(self.artists), len(self.albums))

    def has_album(self, artist: str, album: str) -> bool:
        return (norm(artist), norm(album)) in self.albums

    def has_artist(self, artist: str) -> bool:
        return norm(artist) in self.artists


def declined_albums(conn, logger) -> set[str]:
    """Albums not to suggest again: everything you skipped in earlier lists."""
    declined = set(json.loads(m.get_state(conn, DECLINED_KEY) or "[]"))
    previous, _ = dp.load(conn)
    new = {f"{norm(s.artist)}|{norm(s.title)}" for s in previous if s.status == "open"}
    if new - declined:
        logger.info("Not suggesting again (skipped last time): %d albums", len(new - declined))
    return declined | new


def pick_for_artist(artist: str, reason: str, lib: Library, declined: set[str], seen: set[str],
                    logger, n: int) -> Optional[dp.Suggestion]:
    """The first of the artist's popular albums that is on Tidal, a real album, and not owned."""
    tries = 0
    for album in top_albums(artist, limit=10):
        if tries >= TRIES_PER_ARTIST:
            break
        if lib.has_album(artist, album) or SKIP_TITLE_RE.search(album):
            continue
        tries += 1
        status, match = dp.resolve(artist, album, MIN_TRACKS)
        if status == "api_error":
            raise RuntimeError("Tidal API error (auth?)")
        if status != "ok":
            logger.info("  skip %s - %s (%s)", artist, album, status)
            continue
        album_id, title, tidal_artist, tracks = match
        key = f"{norm(tidal_artist)}|{norm(title)}"
        if key in seen or key in declined or lib.has_album(tidal_artist, title) or SKIP_TITLE_RE.search(title):
            logger.info("  skip %s - %s (owned, skipped before, or not an album)", tidal_artist, title)
            continue
        seen.add(key)
        logger.info("  + %s - %s (%d tracks, %s)", tidal_artist, title, tracks, reason)
        return dp.Suggestion(n=n, artist=tidal_artist, title=title, album_id=album_id,
                             tracks=tracks, reason=reason)
    return None


def build(logger, declined: set[str]) -> list[dp.Suggestion]:
    lib = Library(logger)
    top = m.lastfm_top_artists(limit=10, period="7day")
    logger.info("Top artists this week: %s", ", ".join(top) or "(none)")
    seen: set[str] = set()
    from_top, from_similar = [], []

    for artist in top:
        if len(from_top) >= FROM_TOP:
            break
        s = pick_for_artist(artist, "you play them", lib, declined, seen, logger, 0)
        if s:
            from_top.append(s)

    tried: set[str] = set()
    for artist in top:
        per_source = 0
        for similar in m.lastfm_similar_artists(artist, limit=6):
            if len(from_similar) >= MAX_SUGGESTIONS - len(from_top) or per_source >= SIMILAR_PER_ARTIST:
                break
            if norm(similar) in tried or lib.has_artist(similar):
                continue
            tried.add(norm(similar))
            s = pick_for_artist(similar, f"like {artist}", lib, declined, seen, logger, 0)
            if s:
                from_similar.append(s)
                per_source += 1

    picks = (from_top + from_similar)[:MAX_SUGGESTIONS]
    for i, s in enumerate(picks, 1):
        s.n = i
    return picks


def message(picks: list[dp.Suggestion]) -> str:
    lines = [f"{s.n}. {s.label()} ({s.tracks} tracks · {s.reason})" for s in picks]
    lines.append("")
    lines.append("Reply here with the numbers you want, e.g. 1,3,5 — or 'all' / 'none'.")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="Weekly discovery suggestions (pick by replying on ntfy)")
    ap.add_argument("--dry-run", action="store_true", help="build and print the list; do not store or send it")
    args = ap.parse_args()
    logger = m.setup_logger("discovery", "discovery.log")
    logger.info("========== Discovery suggestions ==========")
    if not m.LASTFM_API_KEY:
        logger.error("No LASTFM_API_KEY")
        return 1
    # The owned-on-disk check needs the drive; without it everything looks missing.
    if not m.ensure_library(logger, "discovery suggestions"):
        return 2
    m.ensure_tidal_token(logger)
    started = time.time()
    conn = m.db_connect()
    declined = declined_albums(conn, logger)
    try:
        picks = build(logger, declined)
    except RuntimeError as exc:
        logger.error("%s", exc)
        m.notify("Discovery", f"Could not build this week's list: {exc}", "warning", "high")
        conn.close()
        return 1
    logger.info("%d suggestions in %ds", len(picks), time.time() - started)
    text = message(picks) if picks else "Nothing new to suggest this week."
    print(text)
    if args.dry_run:
        conn.close()
        return 0
    m.set_state(conn, DECLINED_KEY, json.dumps(sorted(declined)))
    dp.save(picks, conn)
    conn.close()
    if picks:
        m.notify(f"Discovery: {len(picks)} albums for you", text, "mag,headphones")
    return 0


if __name__ == "__main__":
    sys.exit(main())

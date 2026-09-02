#!/usr/bin/env python3
"""Build the "Weekly Discoveries" Navidrome playlist.

Replaces auto-recommendations.sh (deleted in June, timer kept failing).

How it picks songs:
  1. Your Last.fm top artists of the last 7 days (falls back to Navidrome's
     all-time most-played artists if Last.fm is unreachable).
  2. Last.fm "similar artists" for each of those.
  3. Songs by those similar artists that are already in the library,
     preferring tracks you have never played, spread across artists.
  4. Replaces the previous "Weekly Discoveries" playlist and pings ntfy.

Usage: weekly_playlist.py [--dry-run] [--size 25] [--top 5] [--per-artist 3]
"""
from __future__ import annotations

import argparse
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import musiclib as m  # noqa: E402

PLAYLIST_NAME = "Weekly Discoveries"


def navidrome_top_artists(limit: int) -> list[str]:
    rows = m.navidrome_sql(
        "SELECT mf.artist AS name, SUM(an.play_count) AS plays FROM media_file mf "
        "JOIN annotation an ON an.item_id = mf.id AND an.item_type = 'media_file' "
        "WHERE mf.missing = 0 GROUP BY lower(mf.artist) HAVING plays > 0 "
        f"ORDER BY plays DESC LIMIT {int(limit)}"
    )
    return [r["name"] for r in rows if r.get("name")]


def library_songs_for(artist: str) -> list[dict]:
    """Songs in the library whose artist or album artist matches (case-insensitive)."""
    safe = artist.replace("'", "''").lower()
    return m.navidrome_sql(
        "SELECT mf.id, mf.title, mf.artist, mf.album, COALESCE(an.play_count, 0) AS plays "
        "FROM media_file mf LEFT JOIN annotation an "
        "  ON an.item_id = mf.id AND an.item_type = 'media_file' "
        f"WHERE mf.missing = 0 AND (lower(mf.artist) = '{safe}' OR lower(mf.album_artist) = '{safe}')"
    )


def main() -> int:
    ap = argparse.ArgumentParser(description="Weekly Discoveries playlist")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--size", type=int, default=25)
    ap.add_argument("--top", type=int, default=5, help="how many top artists to seed from")
    ap.add_argument("--per-artist", type=int, default=3)
    args = ap.parse_args()

    logger = m.setup_logger("recommendations", "recommendations.log")
    logger.info("=== Weekly Discoveries playlist started ===")

    top = m.lastfm_top_artists(limit=args.top, period="7day") if m.LASTFM_API_KEY else []
    source = "last.fm 7-day"
    if not top:
        logger.warning("Last.fm top artists unavailable; falling back to Navidrome play counts")
        top = navidrome_top_artists(args.top)
        source = "navidrome all-time"
    if not top:
        logger.error("No top artists from any source; nothing to do")
        return 1
    logger.info("Seed artists (%s): %s", source, ", ".join(top))

    top_norm = {m._norm(a) for a in top}
    similar: list[tuple[str, str]] = []  # (similar_artist, via)
    seen = set()
    for artist in top:
        for s in m.lastfm_similar_artists(artist, limit=8):
            key = m._norm(s)
            if key in seen or key in top_norm:
                continue
            seen.add(key)
            similar.append((s, artist))
        time.sleep(0.3)
    logger.info("Similar artists from Last.fm: %d", len(similar))

    # Collect candidate songs, preferring unplayed tracks, per similar artist.
    rng = random.Random()
    pools: list[tuple[str, str, list[dict]]] = []
    for s, via in similar:
        songs = library_songs_for(s)
        if not songs:
            continue
        unplayed = [x for x in songs if not x.get("plays")]
        rng.shuffle(unplayed)
        played = [x for x in songs if x.get("plays")]
        rng.shuffle(played)
        pools.append((s, via, (unplayed + played)[: args.per_artist]))
        logger.info("  %s (via %s): %d songs in library, %d unplayed", s, via, len(songs), len(unplayed))
    if not pools:
        logger.info("No similar artists are in the library yet — nothing to add")
        m.notify("Weekly Discoveries", "No similar-artist tracks in the library this week", "musical_note", "low", logger=logger)
        return 0

    # Round-robin so one artist can't dominate, then cap at --size.
    selected: list[dict] = []
    i = 0
    while len(selected) < args.size and any(pool for _, _, pool in pools):
        for _, _, pool in pools:
            if i < len(pool) and len(selected) < args.size:
                selected.append(pool[i])
        i += 1
        if i > args.per_artist:
            break
    artists_used = sorted({x["artist"] for x in selected}, key=str.lower)
    logger.info("Selected %d songs from %d artists", len(selected), len(artists_used))
    for x in selected:
        logger.info("  + %s - %s (%s) plays=%s", x["artist"], x["title"], x["album"], x.get("plays"))

    if args.dry_run:
        logger.info("Dry run — playlist not written")
        return 0

    sub = m.Subsonic(client="weekly-playlist")
    pid = sub.replace_playlist(PLAYLIST_NAME, [x["id"] for x in selected])
    logger.info("Playlist '%s' written (id=%s) with %d songs", PLAYLIST_NAME, pid, len(selected))
    m.notify("Weekly Discoveries ready",
             f"{len(selected)} songs from {len(artists_used)} artists similar to {', '.join(top[:3])}:\n"
             + m.fmt_list(artists_used, 12),
             "musical_note,sparkles", logger=logger)
    logger.info("=== Done ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())

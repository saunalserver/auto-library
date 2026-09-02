#!/usr/bin/env python3
"""Fetch missing lyrics (.lrc sidecars) for library tracks from LRCLIB.

Replaces the auto-lyrics.sh / fetch-lyrics.sh / process-lyrics.sh trio. Fixes:
  - proper JSON handling (titles with quotes, pipes or backslashes no longer break)
  - remembers every attempt in monitor.db (lyrics_attempts) so tracks LRCLIB does
    not have are not re-queried every night forever (they are retried after 60 days)
  - newest tracks first, so a fresh download has lyrics by the next morning
  - falls back to LRCLIB's /api/search with a duration tolerance when the exact
    /api/get lookup misses
  - verifies the .lrc actually landed on disk before counting it as a success
  - aborts cleanly when the music drive is not mounted

Usage: fetch_lyrics.py [--limit N] [--dry-run] [--retry-days 60]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from difflib import SequenceMatcher
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import musiclib as m  # noqa: E402

LRCLIB = "https://lrclib.net/api"
USER_AGENT = "tidal-monitor-lyrics/2.0 (https://github.com/saunalserver/tidal-monitor)"
DELAY_SECONDS = 0.5
DURATION_TOLERANCE = 4.0  # seconds, for /api/search fallback


def ensure_schema(conn) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS lyrics_attempts (
            path         TEXT PRIMARY KEY,   -- library-relative path (Navidrome's media_file.path)
            status       TEXT NOT NULL,      -- success | not_found | error
            source       TEXT,               -- lrclib-get | lrclib-search
            kind         TEXT,               -- synced | plain
            attempted_at REAL NOT NULL
        )""")
    conn.commit()


def lrclib_get(path: str, params: dict) -> tuple[int, Optional[object]]:
    """GET LRCLIB; retries once with a pause on 429/5xx/network errors. Returns (status, json|None)."""
    url = f"{LRCLIB}/{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    for attempt in (1, 2):
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 404 or exc.code < 500 and exc.code != 429:
                return exc.code, None
            code = exc.code
        except (urllib.error.URLError, TimeoutError, OSError):
            code = 0
        if attempt == 1:
            time.sleep(5)
    return code, None


def _sim(a: str, b: str) -> float:
    return SequenceMatcher(None, m._norm(a), m._norm(b)).ratio()


def lookup(artist: str, title: str, album: str, duration: float) -> tuple[str, Optional[str], Optional[str]]:
    """Return (status, kind, lyrics). status: success | not_found | error."""
    params = {"artist_name": artist, "track_name": title, "duration": int(round(duration))}
    if album:
        params["album_name"] = album
    code, data = lrclib_get("get", params)
    if code == 200 and isinstance(data, dict):
        if data.get("syncedLyrics"):
            return "success", "synced", data["syncedLyrics"]
        if data.get("plainLyrics"):
            return "success", "plain", data["plainLyrics"]
        if data.get("instrumental"):
            return "not_found", None, None
    elif code not in (404,):
        return f"error:http{code}", None, None

    # Fallback: search and accept a close match with a similar duration
    time.sleep(DELAY_SECONDS)
    code, data = lrclib_get("search", {"track_name": title, "artist_name": artist})
    if code != 200 or not isinstance(data, list):
        return (f"error:http{code}" if code != 404 else "not_found"), None, None
    best = None
    for item in data:
        if not isinstance(item, dict):
            continue
        d = item.get("duration") or 0
        if duration and abs(float(d) - duration) > DURATION_TOLERANCE:
            continue
        score = _sim(item.get("trackName", ""), title) * 0.6 + _sim(item.get("artistName", ""), artist) * 0.4
        if score >= 0.75 and (item.get("syncedLyrics") or item.get("plainLyrics")):
            if best is None or score > best[0] or (score == best[0] and item.get("syncedLyrics") and not best[1].get("syncedLyrics")):
                best = (score, item)
    if best:
        item = best[1]
        if item.get("syncedLyrics"):
            return "success", "synced", item["syncedLyrics"]
        return "success", "plain", item["plainLyrics"]
    return "not_found", None, None


def candidates(conn, limit: int, retry_days: int, logger) -> list[dict]:
    """Library tracks without an .lrc, newest first, minus recent failed attempts."""
    rows = m.navidrome_sql(
        "SELECT path, title, artist, album, duration, created_at FROM media_file "
        "WHERE missing = 0 ORDER BY created_at DESC"
    )
    logger.info("Navidrome reports %d tracks", len(rows))
    cutoff_nf = time.time() - retry_days * 86400
    cutoff_err = time.time() - 86400
    attempted = {r["path"]: r for r in conn.execute("SELECT path, status, attempted_at FROM lyrics_attempts")}
    out: list[dict] = []
    skipped_attempted = 0
    for r in rows:
        rel = r["path"]
        if not rel or Path(rel).suffix.lower() not in m.AUDIO_EXTS:
            continue
        lrc = (m.MUSIC_ROOT / rel).with_suffix(".lrc")
        if lrc.exists():
            continue
        prev = attempted.get(rel)
        if prev:
            if prev["status"] == "not_found" and prev["attempted_at"] > cutoff_nf:
                skipped_attempted += 1
                continue
            if prev["status"] == "error" and prev["attempted_at"] > cutoff_err:
                skipped_attempted += 1
                continue
        out.append(r)
        if len(out) >= limit:
            break
    logger.info("Missing .lrc candidates this run: %d (skipped %d recently attempted)", len(out), skipped_attempted)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Fetch missing lyrics from LRCLIB")
    ap.add_argument("--limit", type=int, default=200)
    ap.add_argument("--retry-days", type=int, default=60, help="re-query not_found tracks after this many days")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    logger = m.setup_logger("lyrics", "lyrics.log")
    logger.info("=== Lyrics fetch started (limit %d) ===", args.limit)
    if not m.ensure_library(logger, "lyrics fetch"):
        return 2

    conn = m.db_connect()
    ensure_schema(conn)
    try:
        todo = candidates(conn, args.limit, args.retry_days, logger)
    except Exception as exc:  # noqa: BLE001
        logger.error("Could not list tracks from Navidrome: %s", exc)
        return 1

    ok = nf = err = 0
    for r in todo:
        rel, artist, title, album = r["path"], r.get("artist") or "", r.get("title") or "", r.get("album") or ""
        duration = float(r.get("duration") or 0)
        if not artist or not title:
            continue
        if args.dry_run:
            logger.info("DRY: %s - %s", artist, title)
            continue
        try:
            status, kind, lyrics = lookup(artist, title, album, duration)
        except Exception as exc:  # noqa: BLE001
            logger.warning("lookup failed for %s - %s: %s", artist, title, exc)
            status, kind, lyrics = "error", None, None
        detail = ""
        if status.startswith("error:"):
            status, detail = "error", status.split(":", 1)[1]
        source = None
        if status == "success" and lyrics:
            lrc = (m.MUSIC_ROOT / rel).with_suffix(".lrc")
            try:
                lrc.write_text(lyrics.strip() + "\n", encoding="utf-8")
                if not lrc.exists() or lrc.stat().st_size == 0:
                    raise OSError("write did not land on disk")
                source = "lrclib"
                ok += 1
                logger.info("OK (%s): %s - %s", kind, artist, title)
            except OSError as exc:
                logger.error("Could not write %s: %s — aborting run (drive problem?)", lrc, exc)
                status = "error"
                err += 1
                conn.execute("INSERT OR REPLACE INTO lyrics_attempts VALUES (?,?,?,?,?)",
                             (rel, status, source, kind, time.time()))
                conn.commit()
                break
        elif status == "not_found":
            nf += 1
            logger.info("none: %s - %s", artist, title)
        else:
            err += 1
            logger.warning("error (%s): %s - %s", detail or "exception", artist, title)
        conn.execute("INSERT OR REPLACE INTO lyrics_attempts VALUES (?,?,?,?,?)",
                     (rel, status, source, kind, time.time()))
        conn.commit()
        time.sleep(DELAY_SECONDS)

    logger.info("=== Done: %d written, %d not on LRCLIB, %d errors ===", ok, nf, err)
    if ok and not args.dry_run:
        try:
            m.Subsonic(client="lyrics").start_scan()
            logger.info("Navidrome rescan triggered")
        except Exception as exc:  # noqa: BLE001
            logger.warning("Navidrome rescan failed: %s", exc)
    if err and err >= max(5, len(todo) // 2):
        m.notify("Lyrics fetch errors", f"{err} errors out of {len(todo)} lookups — LRCLIB or drive problem?",
                 "warning", "high", logger=logger)
    conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

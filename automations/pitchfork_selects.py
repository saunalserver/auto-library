#!/usr/bin/env python3
"""Pitchfork Selects automation.

Finds the newest "This Week's Pitchfork Selects Playlist" article, downloads
the albums for tracks the library is missing, and builds a dated Navidrome
playlist.

Runs daily (pitchfork-selects.timer); the article appears Monday afternoon.
Each article is processed once (URL remembered in monitor.db), so every other
day is a single RSS fetch and exit.

Usage: pitchfork_selects.py [--dry-run] [--force] [--url ARTICLE_URL]
"""
from __future__ import annotations

import argparse
import html as html_mod
import logging
import re
import subprocess
import sys
import time
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from difflib import SequenceMatcher
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import List, Optional, Tuple

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
import musiclib as m  # noqa: E402

PITCHFORK_RSS = "https://pitchfork.com/feed/rss"
PITCHFORK_NEWS = "https://pitchfork.com/news/"
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) tidal-monitor"}
STATE_KEY = "pitchfork_last_url"

TIDDL_PYTHON = m.TIDDL_PYTHON
TIDDL_BIN = m.TIDDL_BIN


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


def _fetch(url: str, timeout: int = 30) -> str:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


# ---------------------------------------------------------------------------
# Step 1: find the latest Selects article
# ---------------------------------------------------------------------------

@dataclass
class Article:
    url: str
    title: str
    published: Optional[datetime]


def _extract_selects_links(html: str) -> List[str]:
    """Links on the news page that look like a Selects article.

    Pitchfork moved these from /news/... to /story/... in August 2026; accept
    both, relative or absolute, and require 'selects' in the href itself or
    in the surrounding markup.
    """
    by_href: List[str] = []
    by_context: List[str] = []
    for mt in re.finditer(r'href="((?:https?://pitchfork\.com)?/(?:story|news)/[^"]+)"', html):
        href = mt.group(1)
        if "selects" in href.lower():
            if href not in by_href:
                by_href.append(href)
            continue
        ctx = html[max(0, mt.start() - 300): mt.end() + 300].lower()
        if "selects" in ctx and "pitchfork" in ctx and href not in by_context:
            by_context.append(href)
    # An explicit slug match is reliable; the context heuristic is only a fallback.
    return by_href or by_context


def find_selects_article(logger: logging.Logger) -> Optional[Article]:
    # RSS first: stable structure and gives the publish date.
    try:
        root = ET.fromstring(_fetch(PITCHFORK_RSS))
        best: Optional[Article] = None
        for item in root.iter("item"):
            title = item.findtext("title") or ""
            link = (item.findtext("link") or "").strip()
            if "pitchfork selects" not in title.lower() or not link:
                continue
            pub = None
            try:
                pub = parsedate_to_datetime(item.findtext("pubDate") or "")
            except Exception:  # noqa: BLE001
                pass
            art = Article(url=link, title=title, published=pub)
            if best is None or (pub and best.published and pub > best.published):
                best = art
        if best:
            logger.info("Selects article (RSS): %s", best.url)
            return best
        logger.info("No Selects item in RSS; trying the news page")
    except Exception as exc:  # noqa: BLE001
        logger.warning("RSS fetch failed (%s); trying the news page", exc)

    try:
        links = _extract_selects_links(_fetch(PITCHFORK_NEWS))
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to fetch Pitchfork news page: %s", exc)
        return None
    if not links:
        logger.error("Could not find a Pitchfork Selects article on the news page")
        return None
    url = links[0] if links[0].startswith("http") else f"https://pitchfork.com{links[0]}"
    logger.info("Selects article (news page): %s", url)
    return Article(url=url, title="", published=None)


# ---------------------------------------------------------------------------
# Step 2: parse the tracklist
# ---------------------------------------------------------------------------

@dataclass
class P4kTrack:
    artist: str
    title: str


# Artist: "Track Title" (straight or curly quotes)
_TRACK_LINE = re.compile(r'^([A-Za-z][\w\s\./&\'\-]+?):\s*[\u201c"\u201d]([^\u201c"\u201d]+)[\u201c"\u201d]')
_SKIP_ARTISTS = (
    "pitchfork selects", "pitchfork may earn", "condé nast", "privacy policy",
    "subscribe", "sign up", "read more", "share", "save", "tags",
)


def _parse_article_html(html: str, logger: logging.Logger) -> List[P4kTrack]:
    body_match = re.search(r"<article[^>]*>(.*?)</article>", html, re.DOTALL | re.IGNORECASE)
    body = body_match.group(1) if body_match else html
    text = html_mod.unescape(re.sub(r"<[^>]+>", "\n", body))

    tracks: List[P4kTrack] = []
    seen = set()
    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue
        mt = _TRACK_LINE.match(line)
        if not mt:
            continue
        artist, title = mt.group(1).strip(), mt.group(2).strip()
        if len(artist) < 2 or len(artist) > 80:
            continue
        if any(skip in artist.lower() for skip in _SKIP_ARTISTS):
            continue
        key = (normalize(artist), normalize(title))
        if key not in seen:
            seen.add(key)
            tracks.append(P4kTrack(artist=artist, title=title))
    return tracks


def fetch_and_parse_article(url: str, logger: logging.Logger) -> List[P4kTrack]:
    logger.info("Fetching article: %s", url)
    try:
        html = _fetch(url)
    except Exception as exc:  # noqa: BLE001
        logger.error("Failed to fetch article: %s", exc)
        return []
    tracks = _parse_article_html(html, logger)
    logger.info("Parsed %d tracks from article", len(tracks))
    for t in tracks:
        logger.info("  %s: %s", t.artist, t.title)
    if not tracks:
        # Dump the HTML so the regex can be fixed instead of failing silently.
        m.LOG_DIR.mkdir(parents=True, exist_ok=True)
        dump = m.LOG_DIR / f"pitchfork_article_{datetime.now().strftime('%Y%m%d_%H%M%S')}.html"
        try:
            dump.write_text(html, encoding="utf-8")
            logger.warning("No tracks parsed — raw article dumped to %s", dump)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Could not dump raw article: %s", exc)
    return tracks


# ---------------------------------------------------------------------------
# Step 3: library lookups
# ---------------------------------------------------------------------------

# Collaboration separators. Pitchfork writes "Turnstile and Slayyyter"; the
# file tags say "Turnstile; Slayyyter". Split both into name sets to compare.
_ARTIST_SPLIT = re.compile(
    r"\s*(?:;|,|&|/|\+|\bx\b|\band\b|\bwith\b|\bfeat\.?\b|\bfeaturing\b|\bvs\.?\b)\s*",
    re.IGNORECASE,
)
_PUNCT = re.compile(r"[^\w\s]+")


def loose(text: str) -> str:
    """Lowercase, drop punctuation, collapse whitespace.

    'Birds (Slayyyter Version)' and 'BIRDS: SLAYYYTER VERSION' both become
    'birds slayyyter version'.
    """
    return re.sub(r"\s+", " ", _PUNCT.sub(" ", (text or "").lower())).strip()


def artist_names(text: str) -> set:
    """The individual artists in a credit string, loosely normalised."""
    return {loose(part) for part in _ARTIST_SPLIT.split(text or "") if loose(part)}


def titles_match(wanted: str, found: str) -> bool:
    """True when two track titles denote the same recording.

    Exact after loose normalisation, or one contains the other and they are
    close in length — so 'Birds' does NOT match 'BIRDS: DYING FETUS VERSION',
    but 'Birds (Slayyyter Version)' matches 'BIRDS: SLAYYYTER VERSION'.
    """
    a, b = loose(wanted), loose(found)
    if not a or not b:
        return False
    if a == b:
        return True
    if (a in b or b in a) and min(len(a), len(b)) / max(len(a), len(b)) >= 0.6:
        return True
    return similarity(a, b) >= 0.9


def artists_match(wanted: str, found: str) -> bool:
    """True when the credits share an artist (or the primary names are close)."""
    want, have = artist_names(wanted), artist_names(found)
    if want & have:
        return True
    return any(similarity(w, h) > 0.85 for w in want for h in have)


def find_track_in_navidrome(sub: m.Subsonic, track: P4kTrack, logger: logging.Logger) -> Optional[str]:
    """Song ID in Navidrome for this track, or None."""
    primary = next(iter(sorted(artist_names(track.artist), key=len, reverse=True)), track.artist)
    queries = [f"{track.artist} {track.title}", f"{primary} {track.title}", track.title]
    seen_queries = set()
    for query in queries:
        if query in seen_queries:
            continue
        seen_queries.add(query)
        try:
            songs = sub.search_songs(query, count=10)
        except Exception as exc:  # noqa: BLE001
            logger.warning("  Navidrome search failed: %s", exc)
            return None
        for song in songs:
            if titles_match(track.title, song.get("title") or "") and artists_match(
                track.artist, song.get("artist") or ""
            ):
                logger.info("  In library: %s by %s (id=%s)", song.get("title"), song.get("artist"), song.get("id"))
                return str(song["id"])
    return None


def record_download(artist: str, album: str, logger: logging.Logger) -> None:
    try:
        conn = m.db_connect()
        conn.execute(
            "INSERT OR IGNORE INTO downloaded_albums (artist, album, download_date, file_count) "
            "VALUES (?, ?, datetime('now'), ?)",
            (artist, album, m.count_audio_files(artist, album)),
        )
        conn.commit()
        conn.close()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not record download: %s", exc)


def watch_artist_for_album(artist: str, track_title: str, source: str = "pitchfork") -> None:
    """Ask the monitor to watch this artist for a full album (single was downloaded)."""
    try:
        conn = m.db_connect()
        conn.execute(
            "INSERT OR IGNORE INTO album_watch (artist, track_title, added, source) VALUES (?, ?, datetime('now'), ?)",
            (artist, track_title, source),
        )
        conn.commit()
        conn.close()
    except Exception:  # noqa: BLE001
        pass


# ---------------------------------------------------------------------------
# Step 4: Tidal
# ---------------------------------------------------------------------------

def _tiddl_api(code: str, timeout: int = 30) -> subprocess.CompletedProcess:
    prelude = (
        "import sys\n"
        "from tiddl.api import TidalApi\n"
        "from tiddl.config import Config\n"
        "try:\n"
        "    config = Config.fromFile()\n"
        "    api = TidalApi(config.auth.token, config.auth.user_id, config.auth.country_code)\n"
        "except Exception as e:\n"
        "    print(f'API_ERROR: {e}', file=sys.stderr); sys.exit(2)\n"
    )
    return subprocess.run([TIDDL_PYTHON, "-c", prelude + code], capture_output=True, text=True, timeout=timeout)


def search_tidal_track(artist: str, title: str, logger: logging.Logger) -> Optional[Tuple[str, str, str]]:
    """(album_id, album_name, artist_name) for the best Tidal match, ('auth_error', None, None), or None."""
    query = f"{artist} {title}".replace("\\", " ").replace('"', '\\"')
    code = (
        "try:\n"
        f'    results = api.getSearch("{query}")\n'
        "except Exception as e:\n"
        "    print(f'API_ERROR: {e}', file=sys.stderr); sys.exit(2)\n"
        "for track in results.tracks.items[:5]:\n"
        "    artist_name = track.artists[0].name if track.artists else ''\n"
        "    album_name = track.album.title if track.album else ''\n"
        "    album_id = track.album.id if track.album else ''\n"
        "    print(f'TRACK|{track.id}|{artist_name}|{album_name}|{album_id}|{track.title}')\n"
    )
    try:
        result = _tiddl_api(code)
    except Exception as exc:  # noqa: BLE001
        logger.warning("    Tidal search failed: %s", exc)
        return None
    if result.returncode == 2 or "API_ERROR" in result.stderr:
        logger.error("    Tidal API error: %s", result.stderr.strip()[:200])
        return "auth_error", None, None
    for line in result.stdout.strip().split("\n"):
        parts = line.split("|")
        if len(parts) >= 6 and parts[0] == "TRACK":
            found_artist, found_title = parts[2], parts[5]
            if (similarity(normalize(found_artist), normalize(artist)) > 0.5
                    and similarity(normalize(found_title), normalize(title)) > 0.5):
                logger.info("    Tidal match: %s - %s (album: %s, id: %s)", found_artist, found_title, parts[3], parts[4])
                return parts[4], parts[3], found_artist
    return None


def get_album_track_count(album_id: str) -> int:
    code = (
        "try:\n"
        f"    print(api.getAlbum({int(album_id)}).numberOfTracks)\n"
        "except Exception:\n"
        "    print(0)\n"
    )
    try:
        result = _tiddl_api(code, timeout=15)
        out = result.stdout.strip()
        return int(out) if out.isdigit() else 0
    except Exception:  # noqa: BLE001
        return 0


def download_album_by_id(album_id: str, logger: logging.Logger) -> bool:
    logger.info("    Downloading album %s via tiddl...", album_id)
    try:
        result = subprocess.run([TIDDL_BIN, "url", f"album/{album_id}", "download"],
                                capture_output=True, text=True, timeout=1800)
        if result.returncode == 0:
            return True
        logger.warning("    tiddl exit %s: %s", result.returncode, (result.stderr or result.stdout)[-300:])
        return False
    except Exception as exc:  # noqa: BLE001
        logger.error("    Download exception: %s", exc)
        return False


def download_track_album(track: P4kTrack, logger: logging.Logger) -> Tuple[Optional[str], Optional[str], str]:
    """Returns (album_name, artist_name, status); status in success|single|not_found|auth_error|download_failed."""
    match = search_tidal_track(track.artist, track.title, logger)
    if not match:
        logger.warning("  Not on Tidal: %s - %s", track.artist, track.title)
        return None, None, "not_found"
    album_id, album_name, artist_name = match
    if album_id == "auth_error":
        return None, None, "auth_error"
    if not album_id:
        return None, None, "not_found"

    track_count = get_album_track_count(album_id)
    is_single = 0 < track_count <= 2
    if not download_album_by_id(album_id, logger):
        return None, None, "download_failed"
    on_disk = m.count_audio_files(artist_name, album_name)
    if on_disk == 0:
        # tiddl said OK but nothing landed — happened for a whole week when the USB drive died.
        logger.error("    tiddl reported success but no files on disk for %s - %s", artist_name, album_name)
        return None, None, "download_failed"
    logger.info("    Download complete: %d files on disk", on_disk)
    if is_single:
        logger.info("    Single (%d track%s) — watching for a full album", track_count, "" if track_count == 1 else "s")
        watch_artist_for_album(artist_name, track.title)
    return album_name, artist_name, "single" if is_single else "success"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="Pitchfork Selects → Tidal → Navidrome playlist")
    ap.add_argument("--dry-run", action="store_true", help="find/parse/match only; no downloads, no playlist")
    ap.add_argument("--force", action="store_true", help="process even if this article was already done")
    ap.add_argument("--url", help="process this article URL instead of discovering the latest")
    args = ap.parse_args()

    logger = m.setup_logger("pitchfork-selects", "pitchfork_selects.log")
    logger.info("=" * 60)
    logger.info("Pitchfork Selects started")

    if args.url:
        article = Article(url=args.url, title="", published=None)
    else:
        article = find_selects_article(logger)
    if not article:
        m.notify("Pitchfork Selects", "Could not find this week's article", "warning", "high", logger=logger)
        return 1

    conn = m.db_connect()
    last_url = m.get_state(conn, STATE_KEY)
    if last_url == article.url and not (args.force or args.dry_run):
        logger.info("Already processed %s — nothing to do", article.url)
        conn.close()
        return 0

    if not m.ensure_library(logger, "Pitchfork Selects"):
        conn.close()
        return 2

    tracks = fetch_and_parse_article(article.url, logger)
    if not tracks:
        m.notify("Pitchfork Selects", "Found the article but could not parse any tracks", "warning", "high", logger=logger)
        conn.close()
        return 1

    date_str = (article.published or datetime.now()).strftime("%Y-%m-%d")
    playlist_name = f"Pitchfork Selects {date_str}"
    sub = m.Subsonic(client="pitchfork-selects")

    if not args.dry_run:
        m.ensure_tidal_token(logger)

    song_ids: List[str] = []
    to_download: List[P4kTrack] = []
    for track in tracks:
        sid = find_track_in_navidrome(sub, track, logger)
        if sid:
            song_ids.append(sid)
        else:
            to_download.append(track)
    logger.info("%d/%d tracks already in library; %d to fetch", len(song_ids), len(tracks), len(to_download))

    if args.dry_run:
        for t in to_download:
            logger.info("  would fetch: %s - %s", t.artist, t.title)
        logger.info("Dry run — playlist '%s' not written", playlist_name)
        conn.close()
        return 0

    downloaded: List[str] = []
    watched: List[str] = []
    failed: List[str] = []
    auth_failed = False
    for track in to_download:
        logger.info("Fetching: %s - %s", track.artist, track.title)
        if auth_failed:
            failed.append(f"{track.artist} - {track.title} (skipped, auth broken)")
            continue
        album_name, artist_name, status = download_track_album(track, logger)
        if status == "auth_error":
            auth_failed = True
            failed.append(f"{track.artist} - {track.title} (auth expired)")
        elif status in ("success", "single"):
            downloaded.append(f"{artist_name} - {album_name}")
            if status == "single":
                watched.append(artist_name)
            record_download(artist_name, album_name, logger)
            time.sleep(3)
        else:
            failed.append(f"{track.artist} - {track.title} ({'not on Tidal' if status == 'not_found' else 'download failed'})")

    if downloaded:
        logger.info("Rescanning Navidrome for %d new albums", len(downloaded))
        sub.rescan(logger)
        for track in to_download:
            sid = find_track_in_navidrome(sub, track, logger)
            if sid and sid not in song_ids:
                song_ids.append(sid)

    if song_ids:
        try:
            pid = sub.replace_playlist(playlist_name, song_ids)
            logger.info("Playlist '%s' written (id=%s) with %d tracks", playlist_name, pid, len(song_ids))
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to write playlist: %s", exc)
    else:
        logger.warning("No songs matched — playlist not created")

    if not auth_failed:
        m.set_state(conn, STATE_KEY, article.url)
    conn.close()

    logger.info("Summary: %d/%d matched, %d albums downloaded, %d failed",
                len(song_ids), len(tracks), len(downloaded), len(failed))
    lines = [f"'{playlist_name}': {len(song_ids)}/{len(tracks)} tracks"]
    if downloaded:
        lines.append(f"Downloaded {len(downloaded)}:")
        lines.append(m.fmt_list(downloaded, 10, "  + "))
    if watched:
        lines.append(f"Watching for full albums: {', '.join(watched)}")
    if failed:
        lines.append(f"Failed ({len(failed)}):")
        lines.append(m.fmt_list(failed, 8, "  x "))
    m.notify("Pitchfork Selects", "\n".join(lines), "headphones,musical_note",
             "high" if auth_failed else "default", logger=logger)
    logger.info("Complete")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(1)
    except Exception as exc:  # noqa: BLE001
        logging.getLogger("pitchfork-selects").error("Fatal: %s", exc, exc_info=True)
        sys.exit(1)

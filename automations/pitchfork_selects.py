#!/usr/bin/env python3
"""
Pitchfork Selects Automation

Fetches the weekly Pitchfork Selects playlist, downloads missing albums
from Tidal, and creates a dated Navidrome playlist.

Schedule: Monday mornings via pitchfork-selects.timer
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple

try:
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env")
except ImportError:
    pass

NTFY_URL = "http://localhost:8093/music"
PITCHFORK_RSS = "https://pitchfork.com/feed/rss"
PITCHFORK_NEWS = "https://pitchfork.com/news/"

SUBSONIC_URL = os.getenv("SUBSONIC_URL", "http://localhost:4534")
SUBSONIC_USER = os.getenv("SUBSONIC_USER", "saunalserver")
SUBSONIC_PASS = os.getenv("SUBSONIC_PASS", "")
SUBSONIC_CLIENT = "pitchfork-selects"

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MUSIC_ROOT = Path(os.getenv("MUSIC_ROOT", "/mnt/photos/flac_music"))
MONITOR_DB = PROJECT_ROOT / "database" / "monitor.db"
SMART_DOWNLOAD = PROJECT_ROOT / "smart_download.py"
NAVIDROME_CONTAINER = "navidrome"
NAVIDROME_DB = "/data/navidrome.db"


def setup_logger() -> logging.Logger:
    log_dir = PROJECT_ROOT / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("pitchfork-selects")
    logger.handlers.clear()
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("[%(asctime)s] %(levelname)s: %(message)s")
    # Only attach a FileHandler when not running under systemd — systemd
    # already captures stdout into the same log file, so attaching both
    # handlers duplicates every line.
    if not os.environ.get("INVOCATION_ID"):
        fh = logging.FileHandler(log_dir / "pitchfork_selects.log")
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    return logger


def notify(title: str, message: str, tags: str = "musical_note", priority: str = "default"):
    try:
        data = message.encode("utf-8")
        req = urllib.request.Request(NTFY_URL, data=data, method="POST")
        req.add_header("Title", title)
        req.add_header("Tags", tags)
        req.add_header("Priority", priority)
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        import sys
        print(f"Ntfy notification failed: {e}", file=sys.stderr)


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


# ---------------------------------------------------------------------------
# Step 1: Find the latest Pitchfork Selects article URL
# ---------------------------------------------------------------------------

def find_selects_url(logger: logging.Logger) -> Optional[str]:
    """Find the latest Pitchfork Selects article URL via the news page."""
    logger.info("Fetching Pitchfork news page to find Selects article")
    req = urllib.request.Request(
        PITCHFORK_NEWS,
        headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) tidal-monitor"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            html = resp.read().decode("utf-8")
    except Exception as exc:
        logger.error("Failed to fetch Pitchfork news page: %s", exc)
        return None

    # Find /news/ links with "selects" nearby in the surrounding context
    selects_links: List[str] = []
    for m in re.finditer(r'href="(/news/[^"]*)"', html):
        link = m.group(1)
        start = max(0, m.start() - 300)
        end = min(len(html), m.end() + 300)
        context = html[start:end].lower()
        if "selects" in context and "pitchfork" in context:
            selects_links.append(link)

    if selects_links:
        url = f"https://pitchfork.com{selects_links[0]}"
        logger.info("Found Selects article: %s", url)
        return url

    logger.error("Could not find Pitchfork Selects article on news page")
    return None


# ---------------------------------------------------------------------------
# Step 2: Parse the tracklist from the article
# ---------------------------------------------------------------------------

@dataclass
class P4kTrack:
    artist: str
    title: str


def fetch_and_parse_article(url: str, logger: logging.Logger) -> List[P4kTrack]:
    """Fetch a Pitchfork Selects article and extract the tracklist."""
    logger.info("Fetching article: %s", url)
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) tidal-monitor"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            html = resp.read().decode("utf-8")
    except Exception as exc:
        logger.error("Failed to fetch article: %s", exc)
        return []

    # Extract the body content between article tags
    body_match = re.search(
        r'<article[^>]*>(.*?)</article>', html, re.DOTALL | re.IGNORECASE
    )
    body = body_match.group(1) if body_match else html

    # Remove HTML tags for cleaner parsing
    text = re.sub(r"<[^>]+>", "\n", body)
    # Decode HTML entities
    import html as html_mod
    text = html_mod.unescape(text)

    # Find the section after "Pitchfork Selects:" header
    # The tracklist format is: Artist: "Track Title"
    tracks: List[P4kTrack] = []
    seen = set()

    # Pattern: Artist Name: "Track Title" or Artist: "Track" (with various quotes)
    pattern = r'^([A-Za-z][\w\s\./&\'\-]+?):\s*[\u201c"\u201d]([^\u201c"\u201d]+)[\u201c"\u201d]'

    for line in text.split("\n"):
        line = line.strip()
        if not line:
            continue

        match = re.match(pattern, line)
        if match:
            artist = match.group(1).strip()
            title = match.group(2).strip()

            # Filter out non-track lines (headers, etc.)
            if len(artist) < 2 or len(artist) > 80:
                continue
            if any(skip in artist.lower() for skip in [
                "pitchfork selects", "pitchfork may earn", "condé nast",
                "privacy policy", "subscribe", "sign up", "read more",
                "share", "save", "tags",
            ]):
                continue

            key = (normalize(artist), normalize(title))
            if key not in seen:
                seen.add(key)
                tracks.append(P4kTrack(artist=artist, title=title))

    logger.info("Parsed %d tracks from article", len(tracks))
    for t in tracks:
        logger.info("  %s: %s", t.artist, t.title)

    # If we got nothing, dump the raw HTML so the regex can be debugged.
    # Pitchfork tweaks their CMS occasionally and breaks this parser; the
    # dump makes the failure actionable instead of silent.
    if not tracks:
        log_dir = PROJECT_ROOT / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        dump_path = log_dir / f"pitchfork_article_{datetime.now().strftime('%Y%m%d_%H%M%S')}.html"
        try:
            dump_path.write_text(html, encoding="utf-8")
            logger.warning("No tracks parsed — raw article dumped to %s", dump_path)
        except Exception as exc:
            logger.warning("Could not dump raw article: %s", exc)

    return tracks


# ---------------------------------------------------------------------------
# Step 3: Library ownership check (reuse Navidrome DB pattern)
# ---------------------------------------------------------------------------

class Library:
    def __init__(self, logger: logging.Logger):
        self.logger = logger
        self.albums: set = set()
        self.loaded = False

    def load(self):
        if self.loaded:
            return
        cmd = [
            "docker", "exec", NAVIDROME_CONTAINER, "sqlite3", NAVIDROME_DB,
            "SELECT lower(artist) || '|' || lower(album) FROM media_file GROUP BY lower(artist), lower(album);",
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=40)
            if result.returncode == 0:
                for line in result.stdout.splitlines():
                    if "|" in line:
                        parts = line.split("|", 1)
                        self.albums.add((normalize(parts[0]), normalize(parts[1])))
                self.logger.info("Library: %d albums loaded from Navidrome", len(self.albums))
        except Exception as exc:
            self.logger.warning("Could not load Navidrome library: %s", exc)
        self.loaded = True

    def is_album_owned(self, artist: str, album: str) -> bool:
        return (normalize(artist), normalize(album)) in self.albums

    def record_download(self, artist: str, album: str):
        self.albums.add((normalize(artist), normalize(album)))
        try:
            import sqlite3
            conn = sqlite3.connect(MONITOR_DB)
            conn.execute(
                "INSERT OR IGNORE INTO downloaded_albums (artist, album, download_date, file_count) "
                "VALUES (?, ?, datetime('now'), 0)",
                (artist, album),
            )
            conn.commit()
            conn.close()
        except Exception as exc:
            self.logger.warning("Could not record download: %s", exc)


# ---------------------------------------------------------------------------
# Step 4: Search Navidrome via Subsonic API for track IDs
# ---------------------------------------------------------------------------

def subsonic_search(query: str) -> dict:
    """Search Navidrome via Subsonic API and return raw JSON response."""
    q_enc = urllib.parse.quote(query, safe="")
    url = (
        f"{SUBSONIC_URL}/rest/search3?query={q_enc}"
        f"&artistCount=0&albumCount=0&songCount=5"
        f"&u={SUBSONIC_USER}&p={SUBSONIC_PASS}"
        f"&v=1.16.1&c={SUBSONIC_CLIENT}&f=json"
    )
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data.get("subsonic-response", {}).get("searchResult3", {})
    except Exception:
        return {}


def find_track_in_navidrome(track: P4kTrack, logger: logging.Logger) -> Optional[str]:
    """Search for a specific track in the Navidrome library. Returns song ID or None."""
    # Try "artist track" first
    for query in [f"{track.artist} {track.title}", track.title]:
        result = subsonic_search(query)
        songs = result.get("song", [])
        for song in songs:
            song_artist = song.get("artist", "").lower()
            song_title = song.get("title", "").lower()
            if (
                normalize(track.artist) in song_artist
                and normalize(track.title) in song_title
            ) or (
                normalize(track.title) in song_title
                and similarity(normalize(track.artist), song_artist) > 0.6
            ):
                logger.info("  Found in library: %s by %s (id=%s)", song.get("title"), song.get("artist"), song.get("id"))
                return str(song["id"])
    return None


def similarity(a: str, b: str) -> float:
    from difflib import SequenceMatcher
    return SequenceMatcher(None, a, b).ratio()


# ---------------------------------------------------------------------------
# Step 5: Search Tidal for track's album, then download it
# ---------------------------------------------------------------------------

TIDDL_PYTHON = "/home/saunalserver/.local/share/pipx/venvs/tiddl/bin/python3"
TIDDL_BIN = str(Path.home() / ".local/bin/tiddl")


def search_tidal_track(artist: str, title: str, logger: logging.Logger) -> Optional[Tuple[str, str, str]]:
    """Search Tidal for a track and return (album_id, album_name, artist_name) or None."""
    query = f"{artist} {title}"
    code = f'''
import sys
from tiddl.api import TidalApi
from tiddl.config import Config
try:
    config = Config.fromFile()
    api = TidalApi(config.auth.token, config.auth.user_id, config.auth.country_code)
    results = api.getSearch("{query.replace('"', '\\"')}")
except Exception as e:
    print(f"API_ERROR: {{e}}", file=sys.stderr)
    sys.exit(2)
for track in results.tracks.items[:5]:
    artist_name = track.artists[0].name if track.artists else ""
    album_name = track.album.title if track.album else ""
    album_id = track.album.id if track.album else ""
    print(f"TRACK|{{track.id}}|{{artist_name}}|{{album_name}}|{{album_id}}|{{track.title}}")
'''
    try:
        result = subprocess.run(
            [TIDDL_PYTHON, "-c", code],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 2 or "API_ERROR" in result.stderr:
            error_msg = result.stderr.strip()[:200]
            logger.error("    Tidal API error: %s", error_msg)
            return "auth_error", None, None

        for line in result.stdout.strip().split("\n"):
            parts = line.split("|")
            if len(parts) >= 6 and parts[0] == "TRACK":
                found_artist = parts[2]
                found_title = parts[5]
                # Fuzzy match to make sure we got the right track
                if (
                    similarity(normalize(found_artist), normalize(artist)) > 0.5
                    and similarity(normalize(found_title), normalize(title)) > 0.5
                ):
                    album_id = parts[4]
                    album_name = parts[3]
                    logger.info("    Tidal match: %s - %s (album: %s, id: %s)", found_artist, found_title, album_name, album_id)
                    return album_id, album_name, found_artist
    except Exception as exc:
        logger.warning("    Tidal search failed: %s", exc)
    return None


def get_album_track_count(album_id: str) -> int:
    """Get the number of tracks in a Tidal album."""
    code = f'''
from tiddl.api import TidalApi
from tiddl.config import Config
try:
    config = Config.fromFile()
    api = TidalApi(config.auth.token, config.auth.user_id, config.auth.country_code)
    album = api.getAlbum({album_id})
    print(album.numberOfTracks)
except:
    print("0")
'''
    try:
        result = subprocess.run([TIDDL_PYTHON, "-c", code], capture_output=True, text=True, timeout=15)
        return int(result.stdout.strip()) if result.stdout.strip().isdigit() else 0
    except:
        return 0


def watch_artist_for_album(artist: str, track_title: str, source: str = "pitchfork"):
    """Add an artist to the album_watch table so the monitor checks for full albums later."""
    try:
        import sqlite3
        conn = sqlite3.connect(MONITOR_DB)
        conn.execute(
            "INSERT OR IGNORE INTO album_watch (artist, track_title, added, source) VALUES (?, ?, datetime('now'), ?)",
            (artist, track_title, source),
        )
        conn.commit()
        conn.close()
    except Exception:
        pass


def download_album_by_id(album_id: str, logger: logging.Logger) -> bool:
    """Download a Tidal album by its ID."""
    logger.info("    Downloading album ID %s via tiddl...", album_id)
    try:
        result = subprocess.run(
            [TIDDL_BIN, "url", f"album/{album_id}", "download"],
            capture_output=True, text=True, timeout=1800,
        )
        if result.returncode == 0:
            logger.info("    Download complete")
            return True
        logger.warning("    tiddl exit %s: %s", result.returncode, (result.stderr or result.stdout)[:200])
        return False
    except Exception as exc:
        logger.error("    Download exception: %s", exc)
        return False
def download_track_album(track: P4kTrack, logger: logging.Logger) -> Optional[Tuple[str, str, str]]:
    """Search Tidal for a track, find its album, and download it.
    Returns (album_name, artist_name, status) on attempt.
    status is 'success', 'not_found', 'auth_error', 'single', or 'download_failed'."""
    match = search_tidal_track(track.artist, track.title, logger)
    if not match:
        logger.warning("  Track not found on Tidal: %s - %s", track.artist, track.title)
        return None, None, "not_found"

    album_id, album_name, artist_name = match
    if album_id == "auth_error":
        return None, None, "auth_error"

    if not album_id:
        logger.warning("  No album ID for: %s - %s", track.artist, track.title)
        return None, None, "not_found"

    # Check if this is a single (< 3 tracks) — likely a pre-release single
    track_count = get_album_track_count(album_id)
    is_single = track_count > 0 and track_count <= 2

    success = download_album_by_id(album_id, logger)
    if success:
        if is_single:
            logger.info("    Single (%d track%s) — watching for full album", track_count, "s" if track_count != 1 else "")
            watch_artist_for_album(artist_name, track.title)
        return album_name, artist_name, "single" if is_single else "success"
    return None, None, "download_failed"


def trigger_navidrome_rescan(logger: logging.Logger, max_wait: int = 120):
    """Trigger a Navidrome rescan and wait for it to finish."""
    base = (
        f"{SUBSONIC_URL}/rest/"
        f"u={SUBSONIC_USER}&p={SUBSONIC_PASS}"
        f"&v=1.16.1&c={SUBSONIC_CLIENT}&f=json"
    )
    try:
        urllib.request.urlopen(f"{SUBSONIC_URL}/rest/startScan?{base}", timeout=15)
        logger.info("Navidrome rescan triggered, waiting for completion...")
    except Exception as exc:
        logger.warning("Could not trigger rescan: %s", exc)
        return

    start = time.time()
    while time.time() - start < max_wait:
        time.sleep(5)
        try:
            with urllib.request.urlopen(f"{SUBSONIC_URL}/rest/getScanStatus?{base}", timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            scanning = data.get("subsonic-response", {}).get("scanStatus", {}).get("scanning", False)
            if not scanning:
                elapsed = int(time.time() - start)
                logger.info("Navidrome scan complete (%ds)", elapsed)
                return
        except Exception:
            pass
    logger.warning("Navidrome scan did not complete within %ds, proceeding anyway", max_wait)


# ---------------------------------------------------------------------------
# Step 6: Create Navidrome playlist
# ---------------------------------------------------------------------------

def create_playlist(name: str, song_ids: List[str], logger: logging.Logger):
    """Create a Navidrome playlist with the given song IDs."""
    if not song_ids:
        logger.info("No songs to add to playlist")
        return

    name_enc = urllib.parse.quote(name, safe="")
    params = f"name={name_enc}"
    for sid in song_ids:
        params += f"&songId={sid}"

    url = (
        f"{SUBSONIC_URL}/rest/createPlaylist?{params}"
        f"&u={SUBSONIC_USER}&p={SUBSONIC_PASS}"
        f"&v=1.16.1&c={SUBSONIC_CLIENT}&f=json"
    )
    try:
        urllib.request.urlopen(url, timeout=15)
        logger.info("Created playlist '%s' with %d tracks", name, len(song_ids))
    except Exception as exc:
        logger.error("Failed to create playlist: %s", exc)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    logger = setup_logger()
    logger.info("=" * 60)
    logger.info("Pitchfork Selects automation started")
    logger.info("=" * 60)

    # 1. Find the latest Selects article
    article_url = find_selects_url(logger)
    if not article_url:
        notify("Pitchfork Selects", "Could not find this week's article", "warning", "high")
        return 1

    # 2. Parse the tracklist
    tracks = fetch_and_parse_article(article_url, logger)
    if not tracks:
        notify("Pitchfork Selects", "Found article but could not parse tracks", "warning", "high")
        return 1

    playlist_date = datetime.now().strftime("%Y-%m-%d")
    playlist_name = f"Pitchfork Selects {playlist_date}"

    # 3. Load library
    library = Library(logger)
    library.load()

    # 4. For each track: check Navidrome, download album if missing
    song_ids: List[str] = []
    downloaded_count = 0
    singles_count = 0
    not_found_count = 0
    auth_failed = False
    downloaded_albums: List[str] = []
    watched_artists: List[str] = []
    failed_tracks: List[str] = []

    for track in tracks:
        logger.info("Processing: %s - %s", track.artist, track.title)
        if auth_failed:
            failed_tracks.append(f"{track.artist} - {track.title} (skipped, auth broken)")
            continue

        # Try to find in current library
        track_id = find_track_in_navidrome(track, logger)
        if track_id:
            song_ids.append(track_id)
            continue

        # Not in library — search Tidal for track, find its album, download it
        logger.info("  Not in library, searching Tidal for: %s - %s", track.artist, track.title)
        album_name, artist_name, status = download_track_album(track, logger)
        if status == "auth_error":
            auth_failed = True
            failed_tracks.append(f"{track.artist} - {track.title} (auth expired)")
            continue
        if status in ("success", "single"):
            downloaded_count += 1
            downloaded_albums.append(f"{artist_name} - {album_name}")
            if status == "single":
                singles_count += 1
                watched_artists.append(artist_name)
            library.record_download(artist_name, album_name)
            time.sleep(3)
        else:
            not_found_count += 1
            reason = "not on Tidal" if status == "not_found" else "download failed"
            failed_tracks.append(f"{track.artist} - {track.title} ({reason})")

    # 5. Rescan Navidrome so new downloads show up
    if downloaded_count > 0:
        logger.info("Triggering Navidrome rescan for %d new downloads...", downloaded_count)
        trigger_navidrome_rescan(logger)

        # Reload library and search again for newly downloaded tracks
        library = Library(logger)
        library.load()
        for track in tracks:
            track_id = find_track_in_navidrome(track, logger)
            if track_id and track_id not in song_ids:
                song_ids.append(track_id)
                logger.info("  Post-rescan match: %s - %s", track.artist, track.title)

    # 6. Create playlist (only if we have songs)
    if song_ids:
        create_playlist(playlist_name, song_ids, logger)
    else:
        logger.warning("No songs matched — skipping playlist creation")

    # 7. Summary
    found_count = len(song_ids)
    total = len(tracks)
    logger.info(
        "Summary: %d/%d tracks matched, %d albums downloaded, %d not found",
        found_count, total, downloaded_count, not_found_count,
    )

    # Detailed ntfy notification
    msg_lines = [f"Playlist '{playlist_name}': {found_count}/{total} tracks matched"]
    if downloaded_albums:
        msg_lines.append(f"Downloaded {downloaded_count} albums:")
        for a in downloaded_albums:
            msg_lines.append(f"  + {a}")
    if watched_artists:
        msg_lines.append(f"Watching {singles_count} artists for full albums: {', '.join(watched_artists)}")
    if failed_tracks:
        msg_lines.append(f"Failed ({len(failed_tracks)}):")
        for t in failed_tracks[:10]:
            msg_lines.append(f"  x {t}")

    notify(
        "Pitchfork Selects",
        "\n".join(msg_lines),
        "headphones,musical_note",
    )

    logger.info("=" * 60)
    logger.info("Complete")
    logger.info("=" * 60)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(1)
    except Exception as exc:
        logging.getLogger("pitchfork-selects").error("Fatal: %s", exc, exc_info=True)
        sys.exit(1)

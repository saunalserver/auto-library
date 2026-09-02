"""Shared helpers for the tidal_auto_monitor automations.

Everything that more than one automation needs lives here so behaviour is
consistent: config from .env, rotating log files, ntfy, the Subsonic API
(token auth, never the plaintext password in a URL), Navidrome's SQLite DB,
the tiddl token, and the "is the music drive actually there?" guard.

Import from project root or automations/ — both add PROJECT_ROOT to sys.path.
"""
from __future__ import annotations

import hashlib
import json
import logging
import logging.handlers
import os
import re
import secrets
import sqlite3
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Iterable, Optional

PROJECT_ROOT = Path(__file__).resolve().parent

try:
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env")
except ImportError:  # dotenv is optional; .env may already be in the environment
    pass

# --- configuration (env overrides, sane defaults) --------------------------
MUSIC_ROOT = Path(os.getenv("MUSIC_ROOT", "/mnt/photos/flac_music"))
LIBRARY_MOUNT = Path(os.getenv("LIBRARY_MOUNT", "/mnt/photos"))
DB_PATH = Path(os.getenv("MONITOR_DB", str(PROJECT_ROOT / "database" / "monitor.db")))
LOG_DIR = Path(os.getenv("LOG_DIR", str(PROJECT_ROOT / "logs")))
NTFY_URL = os.getenv("NTFY_URL", "http://localhost:8093/music")
SUBSONIC_URL = os.getenv("SUBSONIC_URL", "http://localhost:4534").rstrip("/")
SUBSONIC_USER = os.getenv("SUBSONIC_USER", "saunalserver")
SUBSONIC_PASS = os.getenv("SUBSONIC_PASS", "")
NAVIDROME_CONTAINER = os.getenv("NAVIDROME_CONTAINER", "navidrome")
NAVIDROME_DB = os.getenv("NAVIDROME_DB", "/data/navidrome.db")
TIDDL_BIN = os.getenv("TIDDL_BINARY", str(Path.home() / ".local" / "bin" / "tiddl"))
TIDDL_PYTHON = os.getenv("TIDDL_PYTHON", str(Path.home() / ".local/share/pipx/venvs/tiddl/bin/python"))
TIDDL_CONFIG = Path(os.getenv("TIDDL_CONFIG", str(Path.home() / "tiddl.json")))
LASTFM_API_KEY = os.getenv("LASTFM_API_KEY", "")
LASTFM_USERNAME = os.getenv("LASTFM_USERNAME", os.getenv("LASTFM_USER", ""))
LASTFM_API_URL = "http://ws.audioscrobbler.com/2.0/"

AUDIO_EXTS = {".flac", ".m4a", ".mp3", ".opus", ".ogg"}

LOG_MAX_BYTES = 2 * 1024 * 1024
LOG_BACKUPS = 3


# --- logging ----------------------------------------------------------------
def setup_logger(name: str, filename: Optional[str] = None, level: int = logging.INFO) -> logging.Logger:
    """Logger that writes to logs/<filename> (rotating, 2 MB x 3) and stdout.

    Under systemd stdout goes to the journal, so `journalctl --user -u NAME`
    and the file both work, and nothing is written twice.
    """
    logger = logging.getLogger(name)
    logger.handlers.clear()
    logger.setLevel(level)
    logger.propagate = False
    fmt = logging.Formatter("[%(asctime)s] %(levelname)s: %(message)s", "%Y-%m-%d %H:%M:%S")
    if filename:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(
            LOG_DIR / filename, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUPS, encoding="utf-8",
        )
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    return logger


# --- ntfy -------------------------------------------------------------------
def notify(title: str, message: str, tags: str = "musical_note", priority: str = "default",
           url: Optional[str] = None, logger: Optional[logging.Logger] = None) -> bool:
    """POST a notification to ntfy. Never raises."""
    try:
        req = urllib.request.Request((url or NTFY_URL), data=message.encode("utf-8"), method="POST")
        req.add_header("Title", title)
        req.add_header("Tags", tags)
        req.add_header("Priority", priority)
        urllib.request.urlopen(req, timeout=10)
        return True
    except Exception as exc:  # noqa: BLE001 — notifications must never break a run
        if logger:
            logger.warning("ntfy failed: %s", exc)
        return False


# --- music drive guard --------------------------------------------------------
def library_available() -> bool:
    """True only if the music drive is mounted and MUSIC_ROOT is readable and non-empty.

    The library lives on a USB drive that has dropped off the bus before. When
    that happens the mountpoint still exists (an empty directory on the root
    disk), so a naive `exists()` check passes and downloads would vanish into
    the shadow directory. Checking the mount and a real directory listing
    catches both the unmounted and the I/O-error cases.
    """
    try:
        if LIBRARY_MOUNT and not os.path.ismount(LIBRARY_MOUNT):
            return False
        if not MUSIC_ROOT.is_dir():
            return False
        with os.scandir(MUSIC_ROOT) as it:
            return any(True for _ in it)
    except OSError:
        return False


def ensure_library(logger: logging.Logger, what: str = "automation") -> bool:
    """Log + notify (at most once per 6 h across all automations) when the drive is missing."""
    if library_available():
        return True
    logger.error("Music library unavailable: %s is not mounted or unreadable — aborting %s", MUSIC_ROOT, what)
    try:
        conn = db_connect()
        last = float(get_state(conn, "last_library_alert") or 0)
        if time.time() - last > 6 * 3600:
            notify("Music drive missing", f"{MUSIC_ROOT} is not mounted/readable. Skipped: {what}.",
                   "warning,floppy_disk", "high", logger=logger)
            set_state(conn, "last_library_alert", str(int(time.time())))
        conn.close()
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not record library alert: %s", exc)
    return False


# --- monitor.db helpers ---------------------------------------------------------
def db_connect(path: Optional[Path] = None) -> sqlite3.Connection:
    p = Path(path) if path else DB_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p, timeout=60)
    conn.row_factory = sqlite3.Row
    # WAL: readers never block the writer and several automations (monitor,
    # lyrics, dedup scan) can share the DB without "database is locked".
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.OperationalError:
        pass
    conn.execute("CREATE TABLE IF NOT EXISTS daemon_state (key TEXT PRIMARY KEY, value TEXT)")
    return conn


def get_state(conn: sqlite3.Connection, key: str) -> Optional[str]:
    row = conn.execute("SELECT value FROM daemon_state WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None


def set_state(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute("INSERT OR REPLACE INTO daemon_state (key, value) VALUES (?, ?)", (key, value))
    conn.commit()


# --- Navidrome (direct SQLite, read-only use) -------------------------------------
def navidrome_sql(sql: str, timeout: int = 60) -> list[dict]:
    """Run a read-only query against Navidrome's DB inside the container.

    Uses `sqlite3 -json` so titles containing '|' or quotes can't corrupt
    the parse (the old shell scripts split on '|').
    """
    cmd = ["docker", "exec", NAVIDROME_CONTAINER, "sqlite3", "-json", NAVIDROME_DB, sql]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        raise RuntimeError(f"navidrome sqlite3 failed ({result.returncode}): {result.stderr.strip()[:200]}")
    out = result.stdout.strip()
    return json.loads(out) if out else []


# --- Subsonic API ------------------------------------------------------------------
class SubsonicError(RuntimeError):
    pass


class Subsonic:
    """Minimal Subsonic client for Navidrome using salted-token auth."""

    def __init__(self, url: str = SUBSONIC_URL, user: str = SUBSONIC_USER, password: str = SUBSONIC_PASS,
                 client: str = "tidal-monitor", timeout: int = 15):
        self.url = url.rstrip("/")
        self.user = user
        self.password = password
        self.client = client
        self.timeout = timeout

    def _auth_params(self) -> dict:
        salt = secrets.token_hex(8)
        token = hashlib.md5((self.password + salt).encode("utf-8")).hexdigest()
        return {"u": self.user, "t": token, "s": salt, "v": "1.16.1", "c": self.client, "f": "json"}

    def call(self, endpoint: str, params: Optional[dict] = None) -> dict:
        query = dict(params or {})
        query.update(self._auth_params())
        url = f"{self.url}/rest/{endpoint}?{urllib.parse.urlencode(query, doseq=True)}"
        with urllib.request.urlopen(url, timeout=self.timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        body = data.get("subsonic-response", {})
        if body.get("status") != "ok":
            err = body.get("error", {})
            raise SubsonicError(f"{endpoint}: {err.get('message', 'unknown error')} (code {err.get('code')})")
        return body

    def ping(self) -> bool:
        try:
            self.call("ping")
            return True
        except Exception:
            return False

    def search_songs(self, query: str, count: int = 5) -> list[dict]:
        body = self.call("search3", {"query": query, "artistCount": 0, "albumCount": 0, "songCount": count})
        return body.get("searchResult3", {}).get("song", []) or []

    def get_playlists(self) -> list[dict]:
        return self.call("getPlaylists").get("playlists", {}).get("playlist", []) or []

    def find_playlists(self, name: str) -> list[dict]:
        return [p for p in self.get_playlists() if p.get("name") == name]

    def create_playlist(self, name: str, song_ids: Iterable[str]) -> Optional[str]:
        body = self.call("createPlaylist", {"name": name, "songId": list(song_ids)})
        return (body.get("playlist") or {}).get("id")

    def delete_playlist(self, playlist_id: str) -> None:
        self.call("deletePlaylist", {"id": playlist_id})

    def replace_playlist(self, name: str, song_ids: Iterable[str]) -> Optional[str]:
        """Create `name` with `song_ids`, deleting any existing playlist(s) of that name first."""
        for p in self.find_playlists(name):
            self.delete_playlist(p["id"])
        return self.create_playlist(name, song_ids)

    def start_scan(self) -> None:
        self.call("startScan")

    def scanning(self) -> bool:
        body = self.call("getScanStatus")
        return bool(body.get("scanStatus", {}).get("scanning", False))

    def rescan(self, logger: Optional[logging.Logger] = None, max_wait: int = 180) -> bool:
        """Trigger a scan and wait (bounded) for it to finish. Never raises."""
        try:
            self.start_scan()
        except Exception as exc:  # noqa: BLE001
            if logger:
                logger.warning("Navidrome rescan could not be started: %s", exc)
            return False
        start = time.time()
        while time.time() - start < max_wait:
            time.sleep(5)
            try:
                if not self.scanning():
                    if logger:
                        logger.info("Navidrome scan complete (%ds)", int(time.time() - start))
                    return True
            except Exception:  # noqa: BLE001
                pass
        if logger:
            logger.warning("Navidrome scan still running after %ds; continuing", max_wait)
        return False


# --- Last.fm ---------------------------------------------------------------------------
def lastfm_call(params: dict, timeout: int = 25) -> Optional[dict]:
    """GET against the Last.fm API. Returns parsed JSON or None on any failure."""
    merged = {"api_key": LASTFM_API_KEY, "format": "json"}
    merged.update(params)
    url = f"{LASTFM_API_URL}?{urllib.parse.urlencode(merged)}"
    req = urllib.request.Request(url, headers={"User-Agent": "tidal-monitor/2.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if isinstance(data, dict) and "error" in data:
            return None
        return data
    except Exception:  # noqa: BLE001
        return None


def _as_list(items) -> list:
    if items is None:
        return []
    return [items] if isinstance(items, dict) else list(items)


def lastfm_top_artists(limit: int = 10, period: str = "7day", user: Optional[str] = None) -> list[str]:
    data = lastfm_call({"method": "user.gettopartists", "user": user or LASTFM_USERNAME,
                        "limit": str(limit), "period": period})
    if not data:
        return []
    return [a.get("name") for a in _as_list(data.get("topartists", {}).get("artist")) if a.get("name")]


def lastfm_similar_artists(artist: str, limit: int = 5) -> list[str]:
    data = lastfm_call({"method": "artist.getsimilar", "artist": artist, "limit": str(limit), "autocorrect": "1"})
    if not data:
        return []
    return [a.get("name") for a in _as_list(data.get("similarartists", {}).get("artist")) if a.get("name")]


# --- tiddl / Tidal auth ------------------------------------------------------------------
def tidal_token_expiry() -> Optional[int]:
    try:
        cfg = json.loads(TIDDL_CONFIG.read_text())
        return int(cfg.get("auth", {}).get("expires") or 0) or None
    except Exception:  # noqa: BLE001
        return None


def tidal_refresh_token(logger: Optional[logging.Logger] = None) -> bool:
    try:
        result = subprocess.run([TIDDL_BIN, "auth", "refresh"], capture_output=True, text=True, timeout=30)
        ok = result.returncode == 0 or "Refreshed" in result.stdout
        if logger:
            if ok:
                logger.info("Tidal token refreshed")
            else:
                logger.warning("Tidal token refresh failed: %s %s", result.stdout.strip()[:200], result.stderr.strip()[:200])
        return ok
    except Exception as exc:  # noqa: BLE001
        if logger:
            logger.warning("Tidal token refresh error: %s", exc)
        return False


def ensure_tidal_token(logger: Optional[logging.Logger] = None, min_remaining: int = 3600) -> bool:
    """Refresh the Tidal token only when it is missing or expires within `min_remaining` seconds.

    Avoids several automations hammering the refresh endpoint minutes apart.
    """
    expiry = tidal_token_expiry()
    if expiry and expiry - time.time() > min_remaining:
        if logger:
            logger.info("Tidal token valid for %.1f h", (expiry - time.time()) / 3600)
        return True
    return tidal_refresh_token(logger)


# --- filesystem helpers -----------------------------------------------------------------------
_FS_UNSAFE = re.compile(r'[\\/:"*?<>|]+')


def _norm(text: str) -> str:
    """Normalise a name the way it ends up on disk: tiddl strips \\ / : " * ? < > |
    from folder names, so 'NEVER ENOUGH: VERSIONS' is the folder 'NEVER ENOUGH VERSIONS'."""
    return " ".join(_FS_UNSAFE.sub("", (text or "")).strip().lower().split())


def find_album_dirs(artist: str, album: str, root: Path = MUSIC_ROOT) -> list[Path]:
    """All /<artist>/<album>/ folders matching case-insensitively (tiddl sanitises names,
    and Tidal's casing differs from Last.fm's, so an exact path lookup often misses)."""
    artist_n, album_n = _norm(artist), _norm(album)
    if not artist_n or not album_n:
        return []
    out: list[Path] = []
    try:
        for artist_dir in root.iterdir():
            if not artist_dir.is_dir() or _norm(artist_dir.name) != artist_n:
                continue
            for album_dir in artist_dir.iterdir():
                if album_dir.is_dir() and _norm(album_dir.name) == album_n:
                    out.append(album_dir)
    except OSError:
        return []
    return out


def audio_files(directory: Path) -> list[Path]:
    try:
        return sorted(p for p in directory.iterdir() if p.is_file() and p.suffix.lower() in AUDIO_EXTS)
    except OSError:
        return []


def count_audio_files(artist: str, album: str, root: Path = MUSIC_ROOT) -> int:
    return sum(len(audio_files(d)) for d in find_album_dirs(artist, album, root))


def fmt_list(items: Iterable[str], max_items: int = 10, prefix: str = "  ") -> str:
    items = list(items)
    lines = [f"{prefix}{x}" for x in items[:max_items]]
    if len(items) > max_items:
        lines.append(f"{prefix}… and {len(items) - max_items} more")
    return "\n".join(lines)

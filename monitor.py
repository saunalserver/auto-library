#!/usr/bin/env python3
"""
TIDAL Auto-Monitor - Improved version with reliability fixes:
- Auth check before downloads (notifies if broken)
- Failed download tracking with retry
- Notifications on failures, not just success
"""
import sys, sqlite3, subprocess, time, json, re, os, logging
from pathlib import Path
from datetime import datetime
import urllib.request, urllib.parse
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent))
import musiclib as m

NTFY_URL = m.NTFY_URL

try:
    from dedup_lib import (
        index_file, find_existing_fingerprints, fingerprint_file,
        compare_fingerprints, FingerprintResult,
    )
    from dedup_lib import normalize as dedup_normalize
    DEDUP_AVAILABLE = True
except ImportError:
    DEDUP_AVAILABLE = False

load_dotenv()

PROJECT_ROOT = Path(__file__).resolve().parent
DATABASE_PATH = PROJECT_ROOT / 'database' / 'monitor.db'
LOG_DIR = PROJECT_ROOT / 'logs'
LOG_FILE = LOG_DIR / 'monitor.log'
MUSIC_ROOT = Path(os.getenv('MUSIC_ROOT', '/mnt/photos/flac_music'))
TIDDL_BINARY = os.getenv('TIDDL_BINARY', str(Path.home() / '.local' / 'bin' / 'tiddl'))
SMART_DOWNLOAD = str(PROJECT_ROOT / 'smart_download.py')
LASTFM_API_KEY = os.getenv('LASTFM_API_KEY')
LASTFM_USERNAME = os.getenv('LASTFM_USERNAME')
LASTFM_API_URL = 'http://ws.audioscrobbler.com/2.0/'
PLAY_THRESHOLD = 3
NAVIDROME_CONTAINER = "navidrome"
NAVIDROME_DB = "/data/navidrome.db"
MAX_RETRIES = 3
RETRY_BATCH = 10          # failed albums retried per run
WATCH_INTERVAL = 7 * 86400   # re-check a watched artist for a full album weekly...
WATCH_MAX_AGE = 180 * 86400  # ...for up to 6 months

_logger = m.setup_logger('auto-library', LOG_FILE.name)
_LEVELS = {'INFO': logging.INFO, 'WARNING': logging.WARNING, 'ERROR': logging.ERROR}

def log(message, level='INFO'):
    """Rotating logs/monitor.log + stdout (the journal under systemd). Nothing is written twice."""
    if level == 'SUCCESS':
        _logger.info('SUCCESS: %s', message)
    else:
        _logger.log(_LEVELS.get(level, logging.INFO), message)

def notify(title, message, tags="musical_note", priority="default"):
    m.notify(title, message, tags, priority, logger=_logger)

def normalize(text):
    if not text:
        return ""
    return re.sub(r"\s+", " ", text.strip().lower())


def parse_expected_track_count(combined_output):
    """Extract the track count from smart_download's 'Best match' log line.

    smart_download.py prints `Best match: {artist} - {album} ({N} tracks)` on
    success. We use that N to compare against the actual files on disk so
    partial downloads (where tiddl silently dropped tracks) get surfaced
    instead of recorded as a clean success.
    """
    for line in combined_output.split('\n'):
        match = re.search(r'Best match:.*\((\d+)\s*tracks?\)', line, re.IGNORECASE)
        if match:
            return int(match.group(1))
    return None


def count_local_audio_files(artist, album):
    """Count .flac + .m4a files in any /<artist>/<album>/ folder (case-insensitive).

    Returns total across all folder variants that match (handles casing
    differences from Tidal metadata, e.g. 'Charli xcx' vs 'Charli XCX').
    Does NOT match Deluxe/parenthetical variants — exact album name only.
    """
    return m.count_audio_files(artist, album, root=MUSIC_ROOT)


def post_download_dedup_check(conn, artist, album):
    """After a successful download, fingerprint new files and flag duplicates.

    Inserts dedup_findings rows (status='protected') for any new file that
    audio-matches an existing fingerprint. Safe to call repeatedly.
    """
    if not DEDUP_AVAILABLE:
        return
    import sqlite3 as _sqlite3
    import uuid as _uuid
    from base64 import b64decode
    from struct import unpack
    new_files = []
    for album_dir in m.find_album_dirs(artist, album, root=MUSIC_ROOT):
        new_files.extend(m.audio_files(album_dir))
    if not new_files:
        return
    suspect_dupes = []
    for f in new_files:
        try:
            from mutagen import File as MutagenFile
            tags = MutagenFile(str(f))
            title = (tags.get('title', [''])[0] if tags and tags.get('title') else '') or f.stem
            index_file(conn, f, artist, album, title)
            n_artist, n_title = dedup_normalize(artist), dedup_normalize(title)
            rows = find_existing_fingerprints(conn, n_artist, n_title)
            new_fp = fingerprint_file(f)
            for row in rows:
                if Path(row['filepath']).resolve() == f.resolve():
                    continue
                # Reconstruct FingerprintResult from the stored base64. The
                # stored value uses URL-safe base64 with a leading 0x01 format
                # byte that must be stripped before 4-byte-aligned unpacking
                # (mirrors fingerprint_file in dedup_lib).
                raw = b64decode(
                    row['fingerprint'].translate(str.maketrans('-_', '+/'))
                    + '=' * (-len(row['fingerprint']) % 4)
                )
                if raw and raw[0] == 0x01:
                    raw = raw[1:]
                n_ints = len(raw) // 4
                row_fp = FingerprintResult(
                    duration_ms=row['duration_ms'],
                    fingerprint_b64=row['fingerprint'],
                    fingerprint_version=row['fingerprint_version'],
                    raw_ints=unpack(f'>{n_ints}i', raw[:n_ints * 4]),
                )
                sim = compare_fingerprints(new_fp, row_fp)
                if sim >= 0.95:
                    already = conn.execute(
                        "SELECT 1 FROM dedup_findings WHERE filepath = ? AND matched_path = ? AND status != 'deleted'",
                        (str(f), row['filepath'])).fetchone()
                    if already:
                        continue
                    group_id = str(_uuid.uuid4())
                    try:
                        conn.execute("""
                            INSERT INTO dedup_findings
                                (group_id, filepath, artist, album, title, similarity,
                                 matched_path, status, size_bytes, added_at)
                            VALUES (?, ?, ?, ?, ?, ?, ?, 'protected', ?, datetime('now'))
                        """, (group_id, str(f), artist, album, title, sim,
                              row['filepath'], f.stat().st_size))
                        suspect_dupes.append((str(f), row['filepath'], sim))
                    except _sqlite3.IntegrityError:
                        pass  # duplicate (group_id, filepath) — already flagged
        except Exception as e:
            log(f'Post-download fingerprint failed for {f}: {e}', 'WARNING')
    conn.commit()
    if suspect_dupes:
        msg = f'{artist} - {album}: {len(suspect_dupes)} tracks already exist elsewhere'
        log(f'DUPLICATE DETECTED: {msg}', 'WARNING')
        notify('Duplicate Downloaded', msg, tags='warning,duplicate', priority='high')
    return len(suspect_dupes)

def get_token_expiry():
    """Read token expiry time from tiddl config."""
    config_paths = [Path.home() / "tiddl.json", Path.home() / ".config" / "tiddl" / "config.json"]
    for path in config_paths:
        if path.exists():
            try:
                with open(path) as f:
                    cfg = json.load(f)
                return cfg.get("auth", {}).get("expires")
            except Exception:
                pass
    return None

def try_refresh_token():
    """Attempt to refresh the tiddl auth token non-interactively."""
    try:
        result = subprocess.run(
            [TIDDL_BINARY, "auth", "refresh"],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0 or "Refreshed" in result.stdout:
            log("Token refreshed successfully")
            return True
        log(f"Token refresh failed: {result.stdout.strip()} {result.stderr.strip()}", "WARNING")
        return False
    except Exception as e:
        log(f"Token refresh error: {e}", "WARNING")
        return False

def check_tiddl_patch():
    """Verify tiddl exceptions.py has the **_ patch applied.

    tiddl's api.py does `raise ApiError(**data)` where data is the raw Tidal
    response, but the upstream constructor only accepts (status, subStatus,
    userMessage). Any extra key in the response (timestamp, path, ...) makes
    the constructor raise TypeError — which tiddl's download loop swallows
    per-track, silently leaving albums half-downloaded. Upstream marked this
    wontfix (issue #351). We patch exceptions.py locally; this check catches
    a missing patch (typically after a pipx upgrade) before it corrupts more
    downloads.
    """
    TIDDL_PYTHON = os.getenv(
        'TIDDL_PYTHON',
        str(Path.home() / ".local/share/pipx/venvs/tiddl/bin/python"),
    )
    code = (
        "from tiddl.exceptions import ApiError, AuthError\n"
        "try:\n"
        "    ApiError(status=404, subStatus='x', userMessage='y', extra='ignored')\n"
        "    AuthError(status=401, error='x', sub_status='y', error_description='z', extra='ignored')\n"
        "    print('OK')\n"
        "except TypeError as e:\n"
        "    print(f'FAIL: {e}')\n"
    )
    try:
        result = subprocess.run(
            [TIDDL_PYTHON, "-c", code],
            capture_output=True, text=True, timeout=10
        )
        return "OK" in result.stdout
    except Exception as e:
        log(f"Could not verify tiddl patch: {e}", "WARNING")
        return False


def check_tiddl_auth():
    """Check tiddl auth with proactive refresh and auto-recovery."""
    # Refresh whenever the token is within 24h of expiry. Tidal tokens are
    # short-lived enough that this typically fires every run.
    expiry = get_token_expiry()
    if expiry and time.time() > expiry - 86400:
        remaining_hours = (expiry - time.time()) / 3600
        if remaining_hours <= 0:
            log("Token expired — refreshing")
        else:
            log(f"Token expires in {remaining_hours:.1f} hours — refreshing")
        if try_refresh_token():
            # Verify the new token works
            expiry = get_token_expiry()
            if expiry:
                log(f"New token valid until {datetime.fromtimestamp(expiry).strftime('%Y-%m-%d %H:%M')}")
        else:
            log("Proactive refresh failed, will retry on next run", "WARNING")

    # Actual auth check via API call
    TIDDL_PYTHON = os.getenv('TIDDL_PYTHON', str(Path.home() / ".local/share/pipx/venvs/tiddl/bin/python"))
    code = '''
from tiddl.api import TidalApi
from tiddl.config import Config
try:
    config = Config.fromFile()
    api = TidalApi(config.auth.token, config.auth.user_id, config.auth.country_code)
    api.getSearch("test")
    print("OK")
except Exception as e:
    print(f"FAIL: {e}")
'''
    try:
        result = subprocess.run(
            [TIDDL_PYTHON, "-c", code],
            capture_output=True, text=True, timeout=15
        )
        if "OK" in result.stdout:
            return True, None
        error = result.stdout.strip() + " " + result.stderr.strip()
        if "401" in error or "expired" in error.lower() or "login" in error.lower():
            # Auto-recovery: try refreshing on auth failure
            log("Auth expired — attempting auto-refresh", "WARNING")
            if try_refresh_token():
                # Re-check after refresh
                result2 = subprocess.run(
                    [TIDDL_PYTHON, "-c", code],
                    capture_output=True, text=True, timeout=15
                )
                if "OK" in result2.stdout:
                    log("Auth restored via auto-refresh")
                    return True, None
                error = result2.stdout.strip() + " " + result2.stderr.strip()
            return False, f"Auth expired, auto-refresh failed: {error[:200]}"
        return False, f"Auth check failed: {error[:200]}"
    except subprocess.TimeoutExpired:
        return False, "Auth check timed out"
    except Exception as e:
        return False, f"Auth check failed: {e}"

def db_connect():
    """SQLite connection with a long busy timeout.

    The dedup scan, lyrics fetch and this monitor all write to monitor.db.
    sqlite3's 5 s default made the monitor die with "database is locked"
    mid-download; WAL plus a 60 s timeout lets them interleave instead.
    """
    conn = sqlite3.connect(DATABASE_PATH, timeout=60)
    # Row (not plain tuples) so dedup_lib's helpers can use row['filepath'].
    # Row still supports integer indexing and unpacking, so the rest of this
    # module is unaffected. Without it every post-download duplicate check
    # died with "tuple indices must be integers".
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=60000")
    except sqlite3.OperationalError:
        pass
    return conn


def init_database():
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = db_connect()
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS tracks
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, artist TEXT, track TEXT, album TEXT,
                  play_count INTEGER DEFAULT 0, first_seen TIMESTAMP, last_played TIMESTAMP,
                  UNIQUE(artist, track, album))''')
    c.execute('''CREATE TABLE IF NOT EXISTS downloaded_albums
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, artist TEXT, album TEXT,
                  download_date TIMESTAMP, file_count INTEGER, UNIQUE(artist, album))''')
    c.execute('''CREATE TABLE IF NOT EXISTS daemon_state (key TEXT PRIMARY KEY, value TEXT)''')
    # New table for failed downloads that need retry
    c.execute('''CREATE TABLE IF NOT EXISTS failed_downloads
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, artist TEXT, album TEXT,
                  fail_count INTEGER DEFAULT 1, first_failed TIMESTAMP, last_failed TIMESTAMP,
                  last_error TEXT, UNIQUE(artist, album))''')
    c.execute('''CREATE TABLE IF NOT EXISTS album_watch
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, artist TEXT, track_title TEXT,
                  added TIMESTAMP, source TEXT, check_count INTEGER DEFAULT 0,
                  UNIQUE(artist, track_title))''')
    # Migrate existing album_watch tables: add check_count if missing.
    c.execute("PRAGMA table_info(album_watch)")
    columns = [row[1] for row in c.fetchall()]
    if 'check_count' not in columns:
        c.execute("ALTER TABLE album_watch ADD COLUMN check_count INTEGER DEFAULT 0")
        log('Migrated album_watch: added check_count column')
    if 'last_checked' not in columns:
        c.execute("ALTER TABLE album_watch ADD COLUMN last_checked REAL")
        log('Migrated album_watch: added last_checked column')
    conn.commit()
    conn.close()
    log('Database initialized')

def get_last_check_time():
    conn = db_connect()
    c = conn.cursor()
    c.execute("SELECT value FROM daemon_state WHERE key='last_check_time'")
    result = c.fetchone()
    conn.close()
    return int(result[0]) if result else int(time.time())

def set_last_check_time(timestamp):
    conn = db_connect()
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO daemon_state (key, value) VALUES ('last_check_time', ?)", (str(timestamp),))
    conn.commit()
    conn.close()

def get_last_auth_alert():
    """Get timestamp of last auth alert to avoid spamming."""
    conn = db_connect()
    c = conn.cursor()
    c.execute("SELECT value FROM daemon_state WHERE key='last_auth_alert'")
    result = c.fetchone()
    conn.close()
    return int(result[0]) if result else 0

def set_last_auth_alert(timestamp):
    conn = db_connect()
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO daemon_state (key, value) VALUES ('last_auth_alert', ?)", (str(timestamp),))
    conn.commit()
    conn.close()

def record_failed_download(artist, album, error_msg):
    """Record a failed download for later retry."""
    conn = db_connect()
    c = conn.cursor()
    c.execute("""INSERT INTO failed_downloads (artist, album, fail_count, first_failed, last_failed, last_error)
                 VALUES (?, ?, 1, datetime('now'), datetime('now'), ?)
                 ON CONFLICT(artist, album) DO UPDATE SET
                     fail_count = fail_count + 1, last_failed = datetime('now'), last_error = ?""",
              (artist, album, error_msg, error_msg))
    conn.commit()
    conn.close()

def get_failed_downloads_for_retry():
    """Get downloads that failed but haven't exceeded retry limit."""
    conn = db_connect()
    c = conn.cursor()
    c.execute("""SELECT artist, album, fail_count FROM failed_downloads
                 WHERE fail_count < ? ORDER BY last_failed ASC LIMIT ?""", (MAX_RETRIES, RETRY_BATCH))
    results = c.fetchall()
    conn.close()
    return [{'artist': r[0], 'album': r[1], 'fail_count': r[2]} for r in results]

def clear_failed_download(artist, album):
    """Remove from failed downloads after successful download."""
    conn = db_connect()
    c = conn.cursor()
    c.execute("DELETE FROM failed_downloads WHERE LOWER(artist)=LOWER(?) AND LOWER(album)=LOWER(?)", (artist, album))
    conn.commit()
    conn.close()

def fetch_recent_scrobbles(since_timestamp, max_pages=5):
    """All scrobbles since `since_timestamp`, following Last.fm's pagination (200/page)."""
    tracks = []
    try:
        page = 1
        while page <= max_pages:
            params = {'method': 'user.getrecenttracks', 'user': LASTFM_USERNAME, 'api_key': LASTFM_API_KEY,
                      'from': str(since_timestamp), 'format': 'json', 'limit': 200, 'page': page}
            url = f'{LASTFM_API_URL}?{urllib.parse.urlencode(params)}'
            with urllib.request.urlopen(url, timeout=30) as response:
                data = json.loads(response.read().decode())
            if 'recenttracks' not in data:
                break
            chunk = data['recenttracks'].get('track', [])
            if isinstance(chunk, dict):
                chunk = [chunk]
            tracks.extend(chunk)
            total_pages = int(data['recenttracks'].get('@attr', {}).get('totalPages', 1) or 1)
            if page >= total_pages:
                break
            page += 1
        scrobbles = []
        for track in tracks:
            if '@attr' in track and 'nowplaying' in track.get('@attr', {}):
                continue
            artist_data = track.get('artist', {})
            artist = artist_data.get('#text', 'Unknown') if isinstance(artist_data, dict) else str(artist_data)
            album_data = track.get('album', {})
            album = album_data.get('#text', 'Unknown') if isinstance(album_data, dict) else str(album_data)
            scrobbles.append({'artist': artist, 'track': track.get('name', 'Unknown'),
                              'album': album, 'timestamp': int(track.get('date', {}).get('uts', 0))})
        return scrobbles
    except Exception as e:
        log(f'Error fetching scrobbles: {e}', 'ERROR')
        return []

def update_play_counts(scrobbles):
    conn = db_connect()
    c = conn.cursor()
    tracks_to_download = []
    for scrobble in scrobbles:
        artist, track, album = scrobble['artist'], scrobble['track'], scrobble['album']
        if not album or album == 'Unknown':
            continue
        c.execute("""INSERT INTO tracks (artist, track, album, play_count, first_seen, last_played)
                     VALUES (?, ?, ?, 1, datetime('now'), datetime('now'))
                     ON CONFLICT(artist, track, album) DO UPDATE SET
                         play_count = play_count + 1, last_played = datetime('now')""", (artist, track, album))
        c.execute('SELECT play_count FROM tracks WHERE artist=? AND track=? AND album=?', (artist, track, album))
        result = c.fetchone()
        if result and result[0] == PLAY_THRESHOLD:
            log(f'Track hit threshold: {artist} - {track} (Album: {album})')
            tracks_to_download.append({'artist': artist, 'track': track, 'album': album})
    conn.commit()
    conn.close()
    return tracks_to_download

class LibraryChecker:
    def __init__(self):
        self.library_albums = set()
        self.library_tracks = {}
        self.downloaded_albums = set()
        self._loaded = False

    def load(self):
        if self._loaded:
            return
        self._load_from_navidrome()
        self._load_download_history()
        self._loaded = True

    def _load_from_navidrome(self):
        cmd = ["docker", "exec", NAVIDROME_CONTAINER, "sqlite3", NAVIDROME_DB,
               "SELECT lower(artist) || '|' || lower(album) || '|' || lower(title) FROM media_file;"]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=40)
            if result.returncode == 0:
                for line in result.stdout.splitlines():
                    parts = line.split("|")
                    if len(parts) >= 3:
                        artist, album, track = parts[0], parts[1], parts[2]
                        a_norm, b_norm, t_norm = normalize(artist), normalize(album), normalize(track)
                        self.library_albums.add((a_norm, b_norm))
                        if t_norm not in self.library_tracks:
                            self.library_tracks[t_norm] = []
                        self.library_tracks[t_norm].append((a_norm, b_norm))
                log(f"Navidrome index: {len(self.library_albums)} albums, {len(self.library_tracks)} tracks")
        except Exception as e:
            log(f"Failed to load Navidrome index: {e}", "WARNING")

    def _load_download_history(self):
        try:
            conn = db_connect()
            c = conn.cursor()
            c.execute("SELECT lower(artist), lower(album) FROM downloaded_albums")
            for artist, album in c.fetchall():
                self.downloaded_albums.add((normalize(artist), normalize(album)))
            conn.close()
            log(f"Download history: {len(self.downloaded_albums)} albums")
        except Exception as e:
            log(f"Failed to load download history: {e}", "WARNING")

    def is_album_owned(self, artist, album):
        key = (normalize(artist), normalize(album))
        if key in self.downloaded_albums:
            return True, "history"
        if key in self.library_albums:
            return True, "navidrome"
        if self._exists_on_disk(artist, album):
            return True, "disk"
        return False, None

    def track_exists_elsewhere(self, artist, track, target_album):
        artist_norm, track_norm = normalize(artist), normalize(track)
        target_album_norm = normalize(target_album)
        if track_norm in self.library_tracks:
            for lib_artist, lib_album in self.library_tracks[track_norm]:
                if lib_artist == artist_norm and lib_album != target_album_norm:
                    return True, lib_album
        return False, None

    def _exists_on_disk(self, artist, album):
        artist_norm, album_norm = normalize(artist), normalize(album)
        if not MUSIC_ROOT.exists():
            return False
        try:
            for artist_dir in MUSIC_ROOT.iterdir():
                if not artist_dir.is_dir():
                    continue
                if normalize(artist_dir.name) != artist_norm:
                    continue
                for album_dir in artist_dir.iterdir():
                    if album_dir.is_dir() and normalize(album_dir.name) == album_norm:
                        return True
        except:
            pass
        return False

    def record_download(self, artist, album, file_count=None):
        key = (normalize(artist), normalize(album))
        self.downloaded_albums.add(key)
        if file_count is None:
            file_count = count_local_audio_files(artist, album)
        try:
            conn = db_connect()
            c = conn.cursor()
            # Insert if missing (preserves UNIQUE constraint); always update
            # file_count so the column reflects reality after each download.
            c.execute(
                "INSERT OR IGNORE INTO downloaded_albums (artist, album, download_date, file_count) "
                "VALUES (?, ?, datetime('now'), ?)",
                (artist, album, file_count),
            )
            c.execute(
                "UPDATE downloaded_albums SET file_count = ? "
                "WHERE LOWER(artist) = LOWER(?) AND LOWER(album) = LOWER(?)",
                (file_count, artist, album),
            )
            conn.commit()
            conn.close()
        except Exception as e:
            log(f"Failed to record download: {e}", "WARNING")

def download_album(artist, album, checker, min_tracks=2):
    log(f'Smart downloading: {artist} - {album}')
    cmd = ['python3', SMART_DOWNLOAD, artist, album, str(min_tracks)]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
        combined_output = result.stdout + result.stderr

        # Log key output for debugging
        for line in combined_output.split('\n'):
            if line.strip() and ('Candidate' in line or 'Best match' in line or 'ERROR' in line or 'Downloading' in line or 'API_Error' in line):
                log(f'  {line.strip()}')

        # Check for auth failure (from new ApiError in smart_download)
        if 'login first' in combined_output.lower() or 'auth expired' in combined_output.lower() or 'api_error' in combined_output.lower():
            log(f'Auth failed during download: {artist} - {album}', 'ERROR')
            record_failed_download(artist, album, "Auth expired")
            return False, "auth_failed"

        # Check for not found
        if 'could not find matching album' in combined_output.lower():
            log(f'Not found on Tidal: {artist} - {album}', 'WARNING')
            record_failed_download(artist, album, "Not found on Tidal")
            return False, "not_found"
        
        if result.returncode == 0 and 'Best match' in combined_output:
            expected = parse_expected_track_count(combined_output)
            actual = count_local_audio_files(artist, album)
            if actual == 0:
                # Singles/deluxe editions land under Tidal's album title, not Last.fm's.
                mt = re.search(r'Best match:\s*(.+?) - (.+) \(\d+ tracks?\)\s*$', combined_output, re.MULTILINE)
                if mt:
                    actual = count_local_audio_files(mt.group(1), mt.group(2))
            if actual == 0:
                # tiddl exited 0 but nothing is on disk. Seen for a whole week
                # when the USB drive died: 21 albums were recorded as owned
                # and never retried. Treat as a failure so it is retried.
                log(f'NO FILES ON DISK after download: {artist} - {album}', 'ERROR')
                record_failed_download(artist, album, "tiddl reported success but no files on disk")
                return False, "download_failed"
            log(f'Successfully downloaded: {artist} - {album}', 'SUCCESS')
            if expected is not None and actual < expected:
                # Partial download — tiddl returned success but the disk has
                # fewer audio files than Tidal advertised. Surface it loudly
                # so silent corruption can't recur.
                msg = f'{artist} - {album}: {actual}/{expected} tracks on disk'
                log(f'PARTIAL DOWNLOAD DETECTED: {msg}', 'WARNING')
                notify('Album Partial Download', msg, 'warning,arrow_down', 'high')
            elif expected is not None:
                log(f'Verified: {actual}/{expected} tracks on disk')
            # Post-download dedup check: fingerprint new files, flag audio-identical
            # matches against existing library. Safe no-op if dedup_lib missing.
            try:
                dedup_conn = db_connect()
                post_download_dedup_check(dedup_conn, artist, album)
                dedup_conn.close()
            except Exception as e:
                log(f'Dedup check failed (non-fatal): {e}', 'WARNING')
            notify('Album Downloaded', f'{artist} - {album}', 'headphones,arrow_down')
            checker.record_download(artist, album, file_count=actual)
            clear_failed_download(artist, album)
            return True, None
        
        error_msg = combined_output[:300] if combined_output else f"Exit code {result.returncode}"
        log(f'Failed: {artist} - {album}', 'ERROR')
        record_failed_download(artist, album, error_msg)
        return False, "download_failed"
    except Exception as e:
        log(f'Exception: {e}', 'ERROR')
        record_failed_download(artist, album, str(e))
        return False, "exception"


def process_downloads(tracks_to_download, checker):
    albums_to_download = {}
    for track in tracks_to_download:
        key = (track['artist'], track['album'])
        if key not in albums_to_download:
            albums_to_download[key] = track
    log(f'Found {len(albums_to_download)} unique albums to check')
    downloaded_count, skipped_count, failed_count = 0, 0, 0
    failed_albums = []  # (artist, album, error_type)

    for (artist, album), track in albums_to_download.items():
        owned, reason = checker.is_album_owned(artist, album)
        if owned:
            log(f'Album already owned ({reason}): {artist} - {album}')
            skipped_count += 1
            continue
        exists, existing_album = checker.track_exists_elsewhere(track['artist'], track['track'], track['album'])
        if exists:
            log(f'Track exists in "{existing_album}": {artist} - {track["track"]}. Skipping: {album}')
            skipped_count += 1
            continue
        success, error_type = download_album(artist, album, checker)
        if success:
            downloaded_count += 1
        else:
            failed_count += 1
            failed_albums.append((artist, album, error_type))
            if error_type == "auth_failed":
                # Stop trying if auth is broken
                log("Auth failed - stopping further downloads", "ERROR")
                break
        time.sleep(5)

    log(f'Summary: {downloaded_count} downloaded, {skipped_count} skipped, {failed_count} failed')

    # Notify with details on failures
    if failed_count > 0:
        lines = []
        for artist, album, error_type in failed_albums:
            reason = {"not_found": "not on Tidal", "auth_failed": "auth expired",
                      "download_failed": "download error", "exception": "exception"}.get(error_type, error_type)
            lines.append(f"  {artist} - {album} ({reason})")
        msg = f"{failed_count} album(s) failed:\n" + "\n".join(lines)
        notify('Download Failures', msg, 'warning', 'high')

    return downloaded_count

def retry_failed_downloads(checker):
    """Retry previously failed downloads."""
    failed = get_failed_downloads_for_retry()
    if not failed:
        # Check for permanently failed albums and notify once
        notify_permanently_failed()
        return 0

    log(f"Retrying {len(failed)} previously failed downloads")
    success_count = 0

    for item in failed:
        artist, album = item['artist'], item['album']
        owned, reason = checker.is_album_owned(artist, album)
        if owned:
            log(f"Retry skip - now owned ({reason}): {artist} - {album}")
            clear_failed_download(artist, album)
            continue

        success, error_type = download_album(artist, album, checker)
        if success:
            success_count += 1
        elif error_type == "auth_failed":
            break
        time.sleep(5)

    if success_count > 0:
        log(f"Retry summary: {success_count} succeeded")

    # After retrying, check for newly permanent failures
    notify_permanently_failed()

    return success_count

def notify_permanently_failed():
    """Notify about albums that hit MAX_RETRIES and won't be retried anymore."""
    conn = db_connect()
    c = conn.cursor()
    c.execute("""SELECT artist, album, fail_count, last_error FROM failed_downloads
                 WHERE fail_count >= ? ORDER BY last_failed DESC""", (MAX_RETRIES,))
    permanent = c.fetchall()
    conn.close()

    if not permanent:
        return

    # Only notify once per 24h for permanent failures
    state_conn = db_connect()
    state_cur = state_conn.cursor()
    state_cur.execute("SELECT value FROM daemon_state WHERE key='last_permanent_alert'")
    row = state_cur.fetchone()
    state_conn.close()
    last_alert = int(row[0]) if row else 0
    if time.time() - last_alert < 86400:
        return

    lines = []
    for artist, album, fails, error in permanent:
        short_error = (error or "unknown")[:60]
        lines.append(f"  {artist} - {album} ({fails}x: {short_error})")
    msg = f"{len(permanent)} album(s) permanently failed:\n" + "\n".join(lines)
    notify('Albums Permanently Failed', msg, 'x', 'high')

    state_conn = db_connect()
    state_cur = state_conn.cursor()
    state_cur.execute("INSERT OR REPLACE INTO daemon_state (key, value) VALUES ('last_permanent_alert', ?)",
                      (str(int(time.time())),))
    state_conn.commit()
    state_conn.close()

def check_single_on_album(album_id, single_title):
    """Check if a watched single appears on a candidate album's tracklist."""
    TIDDL_PYTHON = os.getenv('TIDDL_PYTHON', str(Path.home() / ".local/share/pipx/venvs/tiddl/bin/python"))
    code = '''
from tiddl.api import TidalApi
from tiddl.config import Config
import sys
try:
    config = Config.fromFile()
    api = TidalApi(config.auth.token, config.auth.user_id, config.auth.country_code)
    tracks = api.getAlbumItems(''' + str(album_id) + ''')
    for item in tracks.items:
        if hasattr(item.item, 'title'):
            print(item.item.title)
except Exception as e:
    print(f"API_ERROR: {e}", file=sys.stderr)
    sys.exit(2)
'''
    try:
        result = subprocess.run([TIDDL_PYTHON, "-c", code], capture_output=True, text=True, timeout=15)
        if result.returncode != 0:
            return True  # API error — don't block, let it through
        track_titles = result.stdout.strip().lower().split("\n")
        single_lower = single_title.lower().strip()
        return any(single_lower in t for t in track_titles)
    except Exception:
        return True  # On error, don't block

def check_album_watch(checker):
    """Check watched artists (singles from Pitchfork etc.) for a full album.

    Each entry is checked at most once per WATCH_INTERVAL and dropped after
    WATCH_MAX_AGE — albums usually follow a single by months, so the old
    "28 checks then give up forever" cap silently abandoned every watch after
    a week. Returns the number of albums downloaded.
    """
    now = time.time()
    conn = db_connect()
    c = conn.cursor()
    c.execute("SELECT artist, track_title FROM album_watch "
              "WHERE added IS NOT NULL AND (julianday('now') - julianday(added)) * 86400 > ?", (WATCH_MAX_AGE,))
    expired = c.fetchall()
    for artist, title in expired:
        log(f"Album watch expired after 6 months: {artist} ('{title}')")
    c.execute("DELETE FROM album_watch WHERE added IS NOT NULL AND (julianday('now') - julianday(added)) * 86400 > ?",
              (WATCH_MAX_AGE,))
    c.execute("SELECT DISTINCT artist, track_title FROM album_watch "
              "WHERE last_checked IS NULL OR last_checked < ?", (now - WATCH_INTERVAL,))
    watched = [(row[0], row[1]) for row in c.fetchall()]
    c.execute("SELECT COUNT(*) FROM album_watch")
    total = c.fetchone()[0]
    conn.commit()
    conn.close()

    if not watched:
        if total:
            log(f"Album watch: {total} entries, none due (checked weekly)")
        return 0

    log(f"Checking album watch for {len(watched)} of {total} artists")

    TIDDL_PYTHON = os.getenv('TIDDL_PYTHON', str(Path.home() / ".local/share/pipx/venvs/tiddl/bin/python"))
    found_count = 0

    for artist, watched_single in watched:
        code = '''
from tiddl.api import TidalApi
from tiddl.config import Config
import sys
try:
    config = Config.fromFile()
    api = TidalApi(config.auth.token, config.auth.user_id, config.auth.country_code)
    results = api.getSearch("''' + artist.replace('"', '\\"') + '''")
except Exception as e:
    print(f"API_ERROR: {e}", file=sys.stderr)
    sys.exit(2)
for a in results.artists.items[:3]:
    print(f"ARTIST|{a.id}|{a.name}")
'''
        result = subprocess.run([TIDDL_PYTHON, "-c", code], capture_output=True, text=True, timeout=15)
        conn_mark = db_connect()
        conn_mark.execute("UPDATE album_watch SET check_count = check_count + 1, last_checked = ? "
                          "WHERE artist = ? AND track_title = ?", (now, artist, watched_single))
        conn_mark.commit()
        conn_mark.close()
        if result.returncode == 2:
            log(f"Album watch API error for {artist}", "WARNING")
            continue

        # Find matching artist
        artist_id = None
        for line in result.stdout.strip().split("\n"):
            parts = line.split("|")
            if len(parts) >= 3 and parts[0] == "ARTIST":
                if normalize(parts[2]) == normalize(artist):
                    artist_id = parts[1]
                    break
        if not artist_id:
            continue

        # Get their albums
        code2 = '''
from tiddl.api import TidalApi
from tiddl.config import Config
import sys
try:
    config = Config.fromFile()
    api = TidalApi(config.auth.token, config.auth.user_id, config.auth.country_code)
    results = api.getArtistAlbums(''' + artist_id + ''')
except Exception as e:
    print(f"API_ERROR: {e}", file=sys.stderr)
    sys.exit(2)
for a in results.items[:50]:
    print(f"{a.id}|{a.title}|{a.numberOfTracks}")
'''
        result2 = subprocess.run([TIDDL_PYTHON, "-c", code2], capture_output=True, text=True, timeout=15)
        if result2.returncode == 2:
            continue

        # Find full albums (>2 tracks) we don't own yet
        for line in result2.stdout.strip().split("\n"):
            parts = line.split("|")
            if len(parts) < 3:
                continue
            album_id, album_title, track_count = parts[0], parts[1], int(parts[2]) if parts[2].isdigit() else 0
            if track_count < 3:
                continue
            owned, _ = checker.is_album_owned(artist, album_title)
            if owned:
                continue

            # Verify the watched single is actually on this album
            if watched_single:
                single_on_album = check_single_on_album(album_id, watched_single)
                if not single_on_album:
                    log(f"  Skipping {album_title} — watched single '{watched_single}' not on this album")
                    continue

            log(f"Watch found new album: {artist} - {album_title} ({track_count} tracks)")
            success, error_type = download_album(artist, album_title, checker)
            if success:
                found_count += 1
                notify('Watch Album Downloaded', f'{artist} - {album_title} (full album)', 'headphones,star')
                # Remove only the matched watch entry, not all entries for this artist
                conn2 = db_connect()
                conn2.execute("DELETE FROM album_watch WHERE artist = ? AND track_title = ?", (artist, watched_single))
                conn2.commit()
                conn2.close()
                log(f"Removed {artist} from album watch")
                break  # One new album per artist per run is enough
            time.sleep(5)
        time.sleep(2)

    if found_count:
        log(f"Album watch summary: {found_count} new full albums found")
    return found_count

def main():
    log('='*60)
    log('TIDAL Auto-Monitor Started')
    log('='*60)
    init_database()

    # The library lives on a USB drive that has silently dropped off before.
    # Bail out early (without advancing last_check_time, so no scrobbles are
    # lost) rather than "downloading" into a dead mount.
    if not m.ensure_library(_logger, "TIDAL monitor"):
        return 2

    # Verify tiddl patch BEFORE auth check — auth check itself can trigger
    # the bug. If the patch is missing, every download is silently corrupt.
    if not check_tiddl_patch():
        log("TIDDL PATCH MISSING — downloads will be silently corrupted", "ERROR")
        last_alert = get_last_auth_alert()
        if time.time() - last_alert > 86400:
            notify(
                'TIDAL Patch Missing',
                'tiddl exceptions.py **_ patch is gone (pipx upgrade?). '
                'Albums will download incomplete. Re-apply per LESSONS.md.',
                'warning,bug', 'urgent',
            )
            set_last_auth_alert(int(time.time()))
    else:
        log("tiddl exceptions.py patch verified OK")

    # Check tiddl auth first
    auth_ok, auth_error = check_tiddl_auth()
    if not auth_ok:
        log(f"TIDDL AUTH CHECK FAILED: {auth_error}", "ERROR")
        # Only alert once per 24 hours to avoid spam
        last_alert = get_last_auth_alert()
        if time.time() - last_alert > 86400:
            notify('TIDAL Auth Broken', auth_error, 'warning,lock', 'urgent')
            set_last_auth_alert(int(time.time()))
        # Still update play counts even if we can't download
    else:
        log("TIDDL auth verified OK")
    
    checker = LibraryChecker()
    checker.load()
    last_check = get_last_check_time()
    log(f"Last check: {datetime.fromtimestamp(last_check).strftime('%Y-%m-%d %H:%M:%S')}")
    log(f'Fetching scrobbles for {LASTFM_USERNAME}...')
    scrobbles = fetch_recent_scrobbles(last_check)
    log(f'Found {len(scrobbles)} scrobbles since last check')
    current_time = int(time.time())
    
    downloads = 0
    if not scrobbles:
        log('No new scrobbles')
    else:
        tracks_to_download = update_play_counts(scrobbles)
        log(f'{len(tracks_to_download)} tracks hit the {PLAY_THRESHOLD}-play threshold')
        if tracks_to_download and auth_ok:
            downloads += process_downloads(tracks_to_download, checker)
        elif tracks_to_download and not auth_ok:
            log(f"Skipping {len(tracks_to_download)} downloads due to auth failure", "WARNING")
            # Record them as failed so they'll be retried
            for track in tracks_to_download:
                record_failed_download(track['artist'], track['album'], "Auth was broken")
    
    # Retry failed downloads if auth is OK
    if auth_ok:
        downloads += retry_failed_downloads(checker)
        downloads += check_album_watch(checker) or 0

    if downloads:
        try:
            m.Subsonic(client="auto-library").start_scan()
            log(f"Navidrome rescan triggered ({downloads} new album(s))")
        except Exception as e:
            log(f"Navidrome rescan failed: {e}", "WARNING")

    set_last_check_time(current_time)
    log('='*60)
    log('Complete')
    log('='*60)
    return 0

if __name__ == '__main__':
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        log('Interrupted', 'WARNING')
        sys.exit(1)
    except Exception as e:
        log(f'Fatal error: {e}', 'ERROR')
        import traceback
        log(traceback.format_exc(), 'ERROR')
        sys.exit(1)

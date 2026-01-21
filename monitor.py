#!/usr/bin/env python3
"""
TIDAL Auto-Monitor - Improved version with reliability fixes:
- Auth check before downloads (notifies if broken)
- Failed download tracking with retry
- Notifications on failures, not just success
"""
NTFY_URL = 'http://localhost:8093/music'
import sys, sqlite3, subprocess, time, json, re
from pathlib import Path
from datetime import datetime
import urllib.request, urllib.parse

PROJECT_ROOT = Path(__file__).resolve().parent
DATABASE_PATH = PROJECT_ROOT / 'database' / 'monitor.db'
LOG_DIR = PROJECT_ROOT / 'logs'
LOG_FILE = LOG_DIR / 'monitor.log'
MUSIC_ROOT = Path('/mnt/photos/flac_music')
TIDDL_BINARY = str(Path.home() / '.local' / 'bin' / 'tiddl')
SMART_DOWNLOAD = str(PROJECT_ROOT / 'smart_download.py')
LASTFM_API_KEY = '***REMOVED:LASTFM_API_KEY***'
LASTFM_USERNAME = 'Shlaghetto'
LASTFM_API_URL = 'http://ws.audioscrobbler.com/2.0/'
PLAY_THRESHOLD = 3
NAVIDROME_CONTAINER = "navidrome"
NAVIDROME_DB = "/data/navidrome.db"
MAX_RETRIES = 3

def log(message, level='INFO'):
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    log_entry = f'[{timestamp}] {level}: {message}'
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, 'a') as f:
        f.write(log_entry + '\n')
    print(log_entry)

def notify(title, message, tags="musical_note", priority="default"):
    try:
        data = message.encode("utf-8")
        req = urllib.request.Request(NTFY_URL, data=data, method="POST")
        req.add_header("Title", title)
        req.add_header("Tags", tags)
        req.add_header("Priority", priority)
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        log(f"Ntfy failed: {e}", "WARNING")

def normalize(text):
    if not text:
        return ""
    return re.sub(r"\s+", " ", text.strip().lower())

def check_tiddl_auth():
    """Check if tiddl is authenticated by running a simple search."""
    try:
        result = subprocess.run(
            [TIDDL_BINARY, 'search', 'test', '--help'],
            capture_output=True, text=True, timeout=10
        )
        # If we get "You must login first" anywhere, auth is broken
        if 'login first' in result.stderr.lower() or 'login first' in result.stdout.lower():
            return False, "Auth expired - please run: tiddl auth login"
        return True, None
    except Exception as e:
        return False, f"tiddl check failed: {e}"

def init_database():
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DATABASE_PATH)
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
    conn.commit()
    conn.close()
    log('Database initialized')

def get_last_check_time():
    conn = sqlite3.connect(DATABASE_PATH)
    c = conn.cursor()
    c.execute("SELECT value FROM daemon_state WHERE key='last_check_time'")
    result = c.fetchone()
    conn.close()
    return int(result[0]) if result else int(time.time())

def set_last_check_time(timestamp):
    conn = sqlite3.connect(DATABASE_PATH)
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO daemon_state (key, value) VALUES ('last_check_time', ?)", (str(timestamp),))
    conn.commit()
    conn.close()

def get_last_auth_alert():
    """Get timestamp of last auth alert to avoid spamming."""
    conn = sqlite3.connect(DATABASE_PATH)
    c = conn.cursor()
    c.execute("SELECT value FROM daemon_state WHERE key='last_auth_alert'")
    result = c.fetchone()
    conn.close()
    return int(result[0]) if result else 0

def set_last_auth_alert(timestamp):
    conn = sqlite3.connect(DATABASE_PATH)
    c = conn.cursor()
    c.execute("INSERT OR REPLACE INTO daemon_state (key, value) VALUES ('last_auth_alert', ?)", (str(timestamp),))
    conn.commit()
    conn.close()

def record_failed_download(artist, album, error_msg):
    """Record a failed download for later retry."""
    conn = sqlite3.connect(DATABASE_PATH)
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
    conn = sqlite3.connect(DATABASE_PATH)
    c = conn.cursor()
    c.execute("""SELECT artist, album, fail_count FROM failed_downloads 
                 WHERE fail_count < ? ORDER BY last_failed ASC LIMIT 5""", (MAX_RETRIES,))
    results = c.fetchall()
    conn.close()
    return [{'artist': r[0], 'album': r[1], 'fail_count': r[2]} for r in results]

def clear_failed_download(artist, album):
    """Remove from failed downloads after successful download."""
    conn = sqlite3.connect(DATABASE_PATH)
    c = conn.cursor()
    c.execute("DELETE FROM failed_downloads WHERE LOWER(artist)=LOWER(?) AND LOWER(album)=LOWER(?)", (artist, album))
    conn.commit()
    conn.close()

def fetch_recent_scrobbles(since_timestamp):
    params = {'method': 'user.getrecenttracks', 'user': LASTFM_USERNAME, 'api_key': LASTFM_API_KEY,
              'from': str(since_timestamp), 'format': 'json', 'limit': 200}
    url = f'{LASTFM_API_URL}?{urllib.parse.urlencode(params)}'
    try:
        with urllib.request.urlopen(url, timeout=30) as response:
            data = json.loads(response.read().decode())
        if 'recenttracks' not in data:
            return []
        tracks = data['recenttracks'].get('track', [])
        if isinstance(tracks, dict):
            tracks = [tracks]
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
    conn = sqlite3.connect(DATABASE_PATH)
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
            conn = sqlite3.connect(DATABASE_PATH)
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

    def record_download(self, artist, album):
        key = (normalize(artist), normalize(album))
        self.downloaded_albums.add(key)
        try:
            conn = sqlite3.connect(DATABASE_PATH)
            c = conn.cursor()
            c.execute("INSERT OR IGNORE INTO downloaded_albums (artist, album, download_date, file_count) VALUES (?, ?, datetime('now'), 0)", (artist, album))
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
            if line.strip() and ('Candidate' in line or 'Best match' in line or 'ERROR' in line or 'Downloading' in line):
                log(f'  {line.strip()}')
        
        # Check for auth failure
        if 'login first' in combined_output.lower():
            log(f'Auth failed during download: {artist} - {album}', 'ERROR')
            record_failed_download(artist, album, "Auth expired")
            return False, "auth_failed"
        
        # Check for not found
        if 'could not find matching album' in combined_output.lower():
            log(f'Not found on Tidal: {artist} - {album}', 'WARNING')
            record_failed_download(artist, album, "Not found on Tidal")
            return False, "not_found"
        
        if result.returncode == 0 and 'Best match' in combined_output:
            log(f'Successfully downloaded: {artist} - {album}', 'SUCCESS')
            notify('Album Downloaded', f'{artist} - {album}', 'headphones,arrow_down')
            checker.record_download(artist, album)
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
            if error_type == "auth_failed":
                # Stop trying if auth is broken
                log("Auth failed - stopping further downloads", "ERROR")
                break
        time.sleep(5)
    
    log(f'Summary: {downloaded_count} downloaded, {skipped_count} skipped, {failed_count} failed')
    
    # Notify if there were failures
    if failed_count > 0:
        notify('Download Failures', f'{failed_count} album(s) failed to download', 'warning', 'high')
    
    return downloaded_count

def retry_failed_downloads(checker):
    """Retry previously failed downloads."""
    failed = get_failed_downloads_for_retry()
    if not failed:
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
    return success_count

def main():
    log('='*60)
    log('TIDAL Auto-Monitor Started')
    log('='*60)
    init_database()
    
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
    
    if not scrobbles:
        log('No new scrobbles')
    else:
        tracks_to_download = update_play_counts(scrobbles)
        log(f'{len(tracks_to_download)} tracks hit the {PLAY_THRESHOLD}-play threshold')
        if tracks_to_download and auth_ok:
            process_downloads(tracks_to_download, checker)
        elif tracks_to_download and not auth_ok:
            log(f"Skipping {len(tracks_to_download)} downloads due to auth failure", "WARNING")
            # Record them as failed so they'll be retried
            for track in tracks_to_download:
                record_failed_download(track['artist'], track['album'], "Auth was broken")
    
    # Retry failed downloads if auth is OK
    if auth_ok:
        retry_failed_downloads(checker)
    
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

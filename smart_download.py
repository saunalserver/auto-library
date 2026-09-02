#!/usr/bin/env python3
"""
Smart album downloader - searches Tidal properly and validates before downloading
"""
import sys
import re
import subprocess
import os
from difflib import SequenceMatcher
from pathlib import Path

TIDDL_PYTHON = os.getenv('TIDDL_PYTHON', str(Path.home() / ".local/share/pipx/venvs/tiddl/bin/python"))
TIDDL_BIN = os.getenv('TIDDL_BINARY', str(Path.home() / ".local/bin/tiddl"))

def normalize(text):
    if not text:
        return ""
    text = re.sub(r"[^\w\s]", " ", text.lower())
    return re.sub(r"\s+", " ", text).strip()

def similarity(a, b):
    return SequenceMatcher(None, normalize(a), normalize(b)).ratio()


_EDITION_RE = re.compile(r"[\(\[]([^)\]]*)[\)\]]\s*$")


def edition(title):
    """The trailing parenthetical of an album title, normalized.

    'choke enough (Deluxe)' -> 'deluxe';  'choke enough' -> ''.
    """
    match = _EDITION_RE.search((title or "").strip())
    return normalize(match.group(1)) if match else ""


def base_title(title):
    """The album title with its trailing parenthetical removed, normalized."""
    return normalize(_EDITION_RE.sub("", (title or "").strip()))


def album_score(candidate, wanted):
    """How well a Tidal album title matches the one we asked for, in [0, 1].

    Plain string similarity is not enough: asking for 'choke enough (Deluxe)'
    scored 'choke enough (remixes)' (0.82) above the base album 'choke enough'
    (0.77), purely because 'remixes' shares letters with 'deluxe'. So once the
    base titles agree, decide on the edition instead of on spelling:
    the same edition wins, no edition is an acceptable fallback, and a
    *different* edition is penalised.
    """
    score = similarity(candidate, wanted)
    if base_title(candidate) and base_title(candidate) == base_title(wanted):
        want_ed, have_ed = edition(wanted), edition(candidate)
        if want_ed == have_ed:
            score += 0.15
        elif not have_ed:
            score += 0.05          # base album: safe stand-in for a missing edition
        else:
            score -= 0.15          # a different edition is the wrong record
    return max(0.0, min(1.0, score))

class ApiError(Exception):
    """Raised when the Tidal API call fails (auth, network, etc.)"""
    pass

def search_tidal(query):
    """Search Tidal and return structured results. Raises ApiError on auth/network failures."""
    code = '''
import sys
from tiddl.api import TidalApi
from tiddl.config import Config

try:
    config = Config.fromFile()
    api = TidalApi(config.auth.token, config.auth.user_id, config.auth.country_code)
    results = api.getSearch("''' + query.replace('"', '\\"') + '''")
except Exception as e:
    print(f"API_ERROR: {e}", file=sys.stderr)
    sys.exit(2)

for artist in results.artists.items[:5]:
    print(f"ARTIST|{artist.id}|{artist.name}")

for album in results.albums.items[:10]:
    artist_name = album.artists[0].name if album.artists else ""
    print(f"ALBUM|{album.id}|{album.title}|{artist_name}|{album.numberOfTracks}")

for track in results.tracks.items[:10]:
    artist_name = track.artists[0].name if track.artists else ""
    album_id = track.album.id if track.album else ""
    album_title = track.album.title if track.album else ""
    print(f"TRACK|{track.id}|{track.title}|{artist_name}|{album_id}|{album_title}")
'''
    result = subprocess.run([TIDDL_PYTHON, "-c", code], capture_output=True, text=True, timeout=90)

    if result.returncode == 2 or "API_ERROR" in result.stderr:
        error_msg = result.stderr.strip()
        if "401" in error_msg or "expired" in error_msg.lower():
            raise ApiError(f"Auth expired: {error_msg[:200]}")
        raise ApiError(f"Tidal API error: {error_msg[:200]}")

    artists = []
    albums = []
    tracks = []
    for line in result.stdout.strip().split("\n"):
        parts = line.split("|")
        if len(parts) >= 3 and parts[0] == "ARTIST":
            artists.append({"id": parts[1], "name": parts[2]})
        elif len(parts) >= 5 and parts[0] == "ALBUM":
            albums.append({
                "id": parts[1],
                "title": parts[2],
                "artist": parts[3],
                "track_count": int(parts[4]) if parts[4].isdigit() else 0
            })
        elif len(parts) >= 6 and parts[0] == "TRACK":
            tracks.append({
                "id": parts[1],
                "title": parts[2],
                "artist": parts[3],
                "album_id": parts[4],
                "album_title": parts[5]
            })
    return artists, albums, tracks

def get_artist_albums(artist_id):
    """Get albums for a specific artist"""
    code = '''
from tiddl.api import TidalApi
from tiddl.config import Config

config = Config.fromFile()
api = TidalApi(config.auth.token, config.auth.user_id, config.auth.country_code)
results = api.getArtistAlbums(''' + str(artist_id) + ''')

for album in results.items[:30]:
    print(f"{album.id}|{album.title}|{album.numberOfTracks}")
'''
    result = subprocess.run([TIDDL_PYTHON, "-c", code], capture_output=True, text=True, timeout=90)

    albums = []
    for line in result.stdout.strip().split("\n"):
        parts = line.split("|")
        if len(parts) >= 3:
            albums.append({
                "id": parts[0],
                "title": parts[1],
                "track_count": int(parts[2]) if parts[2].isdigit() else 0
            })
    return albums

def download_album_by_id(album_id):
    result = subprocess.run([TIDDL_BIN, "url", f"album/{album_id}", "download"],
                          capture_output=True, text=True, timeout=1800)
    return result.returncode == 0, result.stdout + result.stderr

def find_best_album_match(artist_name, album_name, min_tracks=2):
    """Find the best matching album on Tidal"""

    best_match = None
    best_score = 0

    # Strategy 1: Search just artist name - this returns their albums in results
    print(f"Searching for artist: {artist_name}")
    artists, albums, _ = search_tidal(artist_name)

    print(f"  Found {len(albums)} albums in search results")

    for album in albums:
        artist_sim = similarity(album["artist"], artist_name)
        album_sim = album_score(album["title"], album_name)
        combined = (artist_sim * 0.4) + (album_sim * 0.6)

        if artist_sim >= 0.5 and album_sim >= 0.5 and album["track_count"] >= min_tracks:
            print(f"  Candidate: {album['artist']} - {album['title']} ({album['track_count']} tracks)")
            print(f"    Artist match: {artist_sim:.0%}, Album match: {album_sim:.0%}")
            if combined > best_score:
                best_score = combined
                best_match = album

    # Strategy 2: Try combined artist+album search
    if not best_match or best_score < 0.7:
        print(f"Trying combined search: {artist_name} {album_name}")
        _, albums, _ = search_tidal(f"{artist_name} {album_name}")

        for album in albums:
            artist_sim = similarity(album["artist"], artist_name)
            album_sim = album_score(album["title"], album_name)
            combined = (artist_sim * 0.4) + (album_sim * 0.6)

            if artist_sim >= 0.5 and album_sim >= 0.5 and album["track_count"] >= min_tracks:
                print(f"  Candidate: {album['artist']} - {album['title']} ({album['track_count']} tracks)")
                if combined > best_score:
                    best_score = combined
                    best_match = album

    # Strategy 3: Search album name directly (risky - needs strong artist match)
    if not best_match or best_score < 0.7:
        print(f"Trying album name search: {album_name}")
        _, albums, _ = search_tidal(album_name)

        for album in albums:
            artist_sim = similarity(album["artist"], artist_name)
            album_sim = album_score(album["title"], album_name)
            combined = (artist_sim * 0.4) + (album_sim * 0.6)

            # Require higher artist match when searching just album name
            if artist_sim >= 0.7 and album_sim >= 0.6 and album["track_count"] >= min_tracks:
                print(f"  Candidate: {album['artist']} - {album['title']} ({album['track_count']} tracks)")
                if combined > best_score:
                    best_score = combined
                    best_match = album

    # Accept album match only if confidence is high enough
    if best_match and best_score >= 0.75:
        return best_match["id"], best_match["title"], best_match["artist"], best_match["track_count"]

    # Strategy 4: Fallback - search for the track and get its parent album
    # This handles singles where Last.fm reports the single name as the "album"
    print(f"Trying track search to find parent album: {artist_name} {album_name}")
    _, _, tracks = search_tidal(f"{artist_name} {album_name}")

    for track in tracks:
        artist_sim = similarity(track["artist"], artist_name)
        track_sim = similarity(track["title"], album_name)
        if artist_sim >= 0.6 and track_sim >= 0.5 and track["album_id"]:
            print(f"  Found track: {track['artist']} - {track['title']} -> album: {track['album_title']}")

            # If the track's parent album meets min_tracks, use it directly
            parent_album_name = track["album_title"]
            parent_sim = similarity(parent_album_name, album_name)

            # Look for a better album by the same artist only if parent is a single/EP
            artists_found, _, _ = search_tidal(track["artist"])
            matching_artist = None
            for a in artists_found:
                if similarity(a["name"], artist_name) >= 0.8:
                    matching_artist = a
                    break

            if matching_artist:
                artist_albums = get_artist_albums(matching_artist["id"])
                # Find albums with names similar to what we're looking for
                # Track count is a tiebreaker, not the selector
                best_alt = None
                best_alt_score = 0
                for a in artist_albums:
                    name_sim = album_score(a["title"], album_name)
                    # Only consider albums whose name matches what we're searching for
                    if name_sim >= 0.6 and a["track_count"] >= min_tracks:
                        # Score: name similarity primary, track count bonus secondary
                        score = name_sim + (min(a["track_count"], 20) / 100.0)
                        if score > best_alt_score:
                            best_alt_score = score
                            best_alt = a
                            print(f"    Alt candidate: {a['title']} ({a['track_count']}t, name_sim={name_sim:.0%})")

                if best_alt:
                    print(f"  Best name-matched album: {best_alt['title']} ({best_alt['track_count']} tracks)")
                    return best_alt["id"], best_alt["title"], track["artist"], best_alt["track_count"]

            # Fall back to the track's parent album ONLY if its name actually
            # matches what we searched for. Otherwise we risk downloading an
            # unrelated compilation (e.g. searching "Hey Baby" and grabbing
            # the artist's "At Play" compilation that the single appears on).
            parent_sim = similarity(track["album_title"], album_name)
            if parent_sim >= 0.5:
                print(f"  Falling back to track's parent album: {track['album_title']} (sim={parent_sim:.0%})")
                return track["album_id"], track["album_title"], track["artist"], -1
            print(f"  Rejecting parent album fallback: '{track['album_title']}' vs '{album_name}' (sim={parent_sim:.0%})")

    # If we had a weak album match from strategies 1-3, return it as last resort
    if best_match:
        print(f"  Warning: accepting low-confidence match (score {best_score:.0%})")
        return best_match["id"], best_match["title"], best_match["artist"], best_match["track_count"]

    return None, None, None, 0

def main():
    if len(sys.argv) < 3:
        print("Usage: smart_download.py <artist> <album> [min_tracks]")
        sys.exit(1)

    artist = sys.argv[1]
    album = sys.argv[2]
    min_tracks = int(sys.argv[3]) if len(sys.argv) > 3 else 2

    print(f"Looking for: {artist} - {album} (min {min_tracks} tracks)")
    print("-" * 50)

    try:
        album_id, found_title, found_artist, track_count = find_best_album_match(artist, album, min_tracks)
    except ApiError as exc:
        # Propagate auth/API errors cleanly so the parent monitor can detect
        # them via stderr grep instead of relying on traceback output.
        print(f"API_ERROR: {exc}", file=sys.stderr)
        sys.exit(2)

    if not album_id:
        print(f"ERROR: Could not find matching album for {artist} - {album}")
        sys.exit(1)

    print(f"\nBest match: {found_artist} - {found_title} ({track_count} tracks)")
    print(f"Album ID: {album_id}")
    print("-" * 50)
    print("Downloading...")

    success, output = download_album_by_id(album_id)
    print(output)

    sys.exit(0 if success else 1)

if __name__ == "__main__":
    main()

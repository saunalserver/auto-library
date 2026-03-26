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

TIDDL_PYTHON = os.getenv('TIDDL_PYTHON', "/usr/bin/python3")
TIDDL_BIN = os.getenv('TIDDL_BINARY', str(Path.home() / ".local/bin/tiddl"))

def normalize(text):
    if not text:
        return ""
    text = re.sub(r"[^\w\s]", " ", text.lower())
    return re.sub(r"\s+", " ", text).strip()

def similarity(a, b):
    return SequenceMatcher(None, normalize(a), normalize(b)).ratio()

def search_tidal(query):
    """Search Tidal and return structured results"""
    code = '''
from tiddl.api import TidalApi
from tiddl.config import Config

config = Config.fromFile()
api = TidalApi(config.auth.token, config.auth.user_id, config.auth.country_code)
results = api.getSearch("''' + query.replace('"', '\\"') + '''")

for artist in results.artists.items[:5]:
    print(f"ARTIST|{artist.id}|{artist.name}")

for album in results.albums.items[:10]:
    artist_name = album.artists[0].name if album.artists else ""
    print(f"ALBUM|{album.id}|{album.title}|{artist_name}|{album.numberOfTracks}")
'''
    result = subprocess.run([TIDDL_PYTHON, "-c", code], capture_output=True, text=True)

    artists = []
    albums = []
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
    return artists, albums

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
    result = subprocess.run([TIDDL_PYTHON, "-c", code], capture_output=True, text=True)

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
    artists, albums = search_tidal(artist_name)

    print(f"  Found {len(albums)} albums in search results")

    for album in albums:
        artist_sim = similarity(album["artist"], artist_name)
        album_sim = similarity(album["title"], album_name)
        combined = (artist_sim * 0.4) + (album_sim * 0.6)

        if artist_sim >= 0.5 and album_sim >= 0.5 and album["track_count"] >= min_tracks:
            print(f"  Candidate: {album['artist']} - {album['title']} ({album['track_count']} tracks)")
            print(f"    Artist match: {artist_sim:.0%}, Album match: {album_sim:.0%}")
            if combined > best_score:
                best_score = combined
                best_match = album

    if best_match:
        return best_match["id"], best_match["title"], best_match["artist"], best_match["track_count"]

    # Strategy 2: Try combined artist+album search
    print(f"Trying combined search: {artist_name} {album_name}")
    _, albums = search_tidal(f"{artist_name} {album_name}")

    for album in albums:
        artist_sim = similarity(album["artist"], artist_name)
        album_sim = similarity(album["title"], album_name)
        combined = (artist_sim * 0.4) + (album_sim * 0.6)

        if artist_sim >= 0.5 and album_sim >= 0.5 and album["track_count"] >= min_tracks:
            print(f"  Candidate: {album['artist']} - {album['title']} ({album['track_count']} tracks)")
            if combined > best_score:
                best_score = combined
                best_match = album

    if best_match:
        return best_match["id"], best_match["title"], best_match["artist"], best_match["track_count"]

    # Strategy 3: Search album name directly (risky - needs strong artist match)
    print(f"Trying album name search: {album_name}")
    _, albums = search_tidal(album_name)

    for album in albums:
        artist_sim = similarity(album["artist"], artist_name)
        album_sim = similarity(album["title"], album_name)

        # Require higher artist match when searching just album name
        if artist_sim >= 0.7 and album_sim >= 0.6 and album["track_count"] >= min_tracks:
            print(f"  Candidate: {album['artist']} - {album['title']} ({album['track_count']} tracks)")
            return album["id"], album["title"], album["artist"], album["track_count"]

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

    album_id, found_title, found_artist, track_count = find_best_album_match(artist, album, min_tracks)

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

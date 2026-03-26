#!/bin/bash
# Generate weekly music recommendations playlist

SCRIPT_DIR="$(dirname "$0")"
LOGFILE="/home/saunalserver/projects/tidal_auto_monitor/logs/recommendations.log"
LASTFM_KEY="***REMOVED:LASTFM_API_KEY***"
SUBSONIC_USER="saunalserver"
SUBSONIC_PASS="***REMOVED:SUBSONIC_PASS***"
SUBSONIC_URL="http://localhost:4534"
PLAYLIST_NAME="Weekly Discoveries"
NTFY_URL="http://localhost:8093/music"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" >> "$LOGFILE"
}

log "Starting recommendations generation"

# Clear temp files
> /tmp/similar_artists.txt
> /tmp/recommendation_songs.txt

# Get top artists and find similar ones
"$SCRIPT_DIR/get-top-artists.sh" 5 | jq -r '.name' | while read -r artist; do
    [ -z "$artist" ] && continue
    log "Getting similar artists for: $artist"

    ARTIST_ENC=$(printf '%s' "$artist" | jq -sRr @uri)
    curl -s "http://ws.audioscrobbler.com/2.0/?method=artist.getsimilar&artist=${ARTIST_ENC}&api_key=${LASTFM_KEY}&format=json&limit=5" | jq -r '.similarartists.artist[].name' 2>/dev/null >> /tmp/similar_artists.txt
    sleep 0.5
done

log "Finding matching artists in library"

# Search for songs from similar artists
cat /tmp/similar_artists.txt | sort -u | head -15 | while read -r similar; do
    [ -z "$similar" ] && continue

    SIMILAR_ENC=$(printf '%s' "$similar" | jq -sRr @uri)
    TRACKS=$(curl -s "${SUBSONIC_URL}/rest/search3?query=${SIMILAR_ENC}&artistCount=0&albumCount=0&songCount=3&u=${SUBSONIC_USER}&p=${SUBSONIC_PASS}&v=1.16.1&c=recommendations&f=json" | jq -r '."subsonic-response".searchResult3.song[]?.id' 2>/dev/null)

    if [ -n "$TRACKS" ]; then
        log "Found tracks for: $similar"
        echo "$TRACKS" >> /tmp/recommendation_songs.txt
    fi
    sleep 0.2
done

# Get unique song IDs
SONG_IDS=$(cat /tmp/recommendation_songs.txt 2>/dev/null | sort -u | head -25)
SONG_COUNT=$(echo "$SONG_IDS" | grep -c . || echo 0)

log "Found $SONG_COUNT songs for playlist"

if [ "$SONG_COUNT" -gt 0 ]; then
    # Delete existing playlist
    EXISTING=$(curl -s "${SUBSONIC_URL}/rest/getPlaylists?u=${SUBSONIC_USER}&p=${SUBSONIC_PASS}&v=1.16.1&c=recommendations&f=json" | jq -r ".\"subsonic-response\".playlists.playlist[] | select(.name==\"$PLAYLIST_NAME\") | .id" 2>/dev/null)

    if [ -n "$EXISTING" ]; then
        curl -s "${SUBSONIC_URL}/rest/deletePlaylist?id=${EXISTING}&u=${SUBSONIC_USER}&p=${SUBSONIC_PASS}&v=1.16.1&c=recommendations&f=json" > /dev/null
        log "Deleted old playlist"
    fi

    # Build songId parameters
    SONG_PARAMS=""
    for id in $SONG_IDS; do
        SONG_PARAMS="${SONG_PARAMS}&songId=${id}"
    done

    # Create playlist
    PLAYLIST_ENC=$(printf '%s' "$PLAYLIST_NAME" | jq -sRr @uri)
    curl -s "${SUBSONIC_URL}/rest/createPlaylist?name=${PLAYLIST_ENC}${SONG_PARAMS}&u=${SUBSONIC_USER}&p=${SUBSONIC_PASS}&v=1.16.1&c=recommendations&f=json" > /dev/null
    log "Created playlist with $SONG_COUNT songs"

    # Notify via Ntfy
    curl -s -H "Title: Weekly Playlist Ready" -H "Tags: musical_note" -d "Your Weekly Discoveries playlist has $SONG_COUNT new songs based on your listening" "$NTFY_URL"
else
    log "No matching songs found in library"
    curl -s -H "Title: Weekly Playlist" -H "Tags: musical_note" -H "Priority: low" -d "No new recommendations this week" "$NTFY_URL"
fi

# Cleanup
rm -f /tmp/similar_artists.txt /tmp/recommendation_songs.txt

log "Completed recommendations generation"

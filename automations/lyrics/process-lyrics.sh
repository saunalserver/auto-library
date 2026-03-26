#!/bin/bash
# Process a single track - fetch lyrics and save .lrc file

ARTIST="$1"
TITLE="$2"
DURATION="$3"
AUDIO_PATH="$4"
MUSIC_DIR="/mnt/photos/flac_music"

LRC_PATH="${MUSIC_DIR}/${AUDIO_PATH%.*}.lrc"

if [ -f "$LRC_PATH" ]; then
    echo '{"status":"skipped","reason":"already_exists"}'
    exit 0
fi

ARTIST_ENC=$(printf "%s" "$ARTIST" | jq -sRr @uri)
TITLE_ENC=$(printf "%s" "$TITLE" | jq -sRr @uri)

LRCLIB_URL="https://lrclib.net/api/get?artist_name=${ARTIST_ENC}&track_name=${TITLE_ENC}&duration=${DURATION}"
RESPONSE=$(curl -s --max-time 10 "$LRCLIB_URL" 2>/dev/null)

if ! echo "$RESPONSE" | jq -e . >/dev/null 2>&1; then
    echo '{"status":"error","reason":"invalid_response"}'
    exit 0
fi

SYNCED=$(echo "$RESPONSE" | jq -r '.syncedLyrics // empty')
if [ -n "$SYNCED" ]; then
    mkdir -p "$(dirname "$LRC_PATH")"
    printf "%s" "$SYNCED" > "$LRC_PATH"
    echo '{"status":"success","source":"lrclib","type":"synced"}'
    exit 0
fi

PLAIN=$(echo "$RESPONSE" | jq -r '.plainLyrics // empty')
if [ -n "$PLAIN" ]; then
    mkdir -p "$(dirname "$LRC_PATH")"
    printf "%s" "$PLAIN" > "$LRC_PATH"
    echo '{"status":"success","source":"lrclib","type":"plain"}'
    exit 0
fi

echo '{"status":"not_found"}'
exit 0

#!/bin/bash
# Fetch tracks that are missing .lrc files

MUSIC_DIR="/mnt/photos/flac_music"
LIMIT="${1:-50}"
count=0

# Get tracks from navidrome container database
docker exec navidrome sqlite3 /data/navidrome.db "
SELECT id, title, artist, album, CAST(duration AS INTEGER), path
FROM media_file 
WHERE (path LIKE '%.flac' OR path LIKE '%.mp3' OR path LIKE '%.m4a')
ORDER BY RANDOM()
" | while IFS='|' read -r id title artist album duration path; do
    lrc_path="${MUSIC_DIR}/${path%.*}.lrc"
    if [ ! -f "$lrc_path" ]; then
        # Escape quotes in title and artist for valid JSON
        title=$(echo "$title" | sed 's/"/\\"/g')
        artist=$(echo "$artist" | sed 's/"/\\"/g')
        album=$(echo "$album" | sed 's/"/\\"/g')
        echo "{\"id\":\"$id\",\"title\":\"$title\",\"artist\":\"$artist\",\"album\":\"$album\",\"duration\":$duration,\"path\":\"$path\"}"
        count=$((count + 1))
        [ "$count" -ge "$LIMIT" ] && break
    fi
done

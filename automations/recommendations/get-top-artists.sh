#!/bin/bash
# Get top played artists from Navidrome

LIMIT="${1:-10}"

# Query most played artists from annotation table (play counts)
docker exec navidrome sqlite3 /data/navidrome.db "
SELECT 
    a.name,
    a.id,
    COALESCE(SUM(an.play_count), 0) as total_plays,
    COUNT(DISTINCT mf.id) as track_count
FROM artist a
LEFT JOIN media_file_artists mfa ON a.id = mfa.artist_id
LEFT JOIN media_file mf ON mfa.media_file_id = mf.id
LEFT JOIN annotation an ON an.item_id = mf.id AND an.item_type = 'media_file'
GROUP BY a.id, a.name
HAVING total_plays > 0
ORDER BY total_plays DESC
LIMIT $LIMIT
" | while IFS='|' read -r name id plays tracks; do
    name=$(echo "$name" | sed 's/"/\\"/g')
    echo "{\"name\":\"$name\",\"id\":\"$id\",\"play_count\":$plays,\"track_count\":$tracks}"
done

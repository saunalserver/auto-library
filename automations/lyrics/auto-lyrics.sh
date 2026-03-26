#!/bin/bash
# Auto-fetch lyrics for tracks missing .lrc files

SCRIPT_DIR="$(dirname "$0")"
LOGFILE="/home/saunalserver/projects/tidal_auto_monitor/logs/lyrics.log"
LIMIT="${1:-50}"
DELAY=1

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" >> "$LOGFILE"
}

log "Starting lyrics fetch - limit: $LIMIT"

SUCCESS=0
FAILED=0

"$SCRIPT_DIR/fetch-lyrics.sh" "$LIMIT" | while IFS= read -r track; do
    [ -z "$track" ] && continue

    ARTIST=$(echo "$track" | jq -r '.artist')
    TITLE=$(echo "$track" | jq -r '.title')
    DURATION=$(echo "$track" | jq -r '.duration')
    TPATH=$(echo "$track" | jq -r '.path')

    log "Processing: $ARTIST - $TITLE"

    RESULT=$("$SCRIPT_DIR/process-lyrics.sh" "$ARTIST" "$TITLE" "$DURATION" "$TPATH")
    STATUS=$(echo "$RESULT" | jq -r '.status')

    if [ "$STATUS" = "success" ]; then
        log "SUCCESS: $ARTIST - $TITLE"
    else
        log "FAILED/SKIPPED: $ARTIST - $TITLE ($STATUS)"
    fi

    sleep $DELAY
done

log "Completed lyrics fetch"

# Trigger Navidrome rescan
curl -s 'http://localhost:4534/rest/startScan?u=saunalserver&p=***REMOVED:SUBSONIC_PASS***&v=1.16.1&c=auto-lyrics&f=json' > /dev/null 2>&1

echo '{"status":"completed"}'

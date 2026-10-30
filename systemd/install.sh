#!/bin/bash
# Install every unit in this directory into the user systemd instance and enable the timers.
# Safe to re-run. Run as the normal user (no sudo): ./systemd/install.sh
#
# The units refer to the repo as %h/projects/auto-library. Cloned there, they are
# symlinked, so the repo stays the single source of truth. Cloned anywhere else,
# a copy with the real path filled in is written instead (re-run after editing a unit).
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(dirname "$HERE")"
DEST="$HOME/.config/systemd/user"
mkdir -p "$DEST"
if [ "$REPO" = "$HOME/projects/auto-library" ]; then MODE=link; else MODE=copy; fi
echo "repo: $REPO  (units: $MODE)"
for f in "$HERE"/*.service "$HERE"/*.timer; do
    name="$(basename "$f")"
    # keep a pre-existing plain copy rather than overwrite it silently
    if [ -e "$DEST/$name" ] && [ ! -L "$DEST/$name" ] && [ "$MODE" = link ]; then
        mv "$DEST/$name" "$DEST/$name.pre-$(date +%Y%m%d)"
    fi
    if [ "$MODE" = link ]; then
        ln -sfn "$f" "$DEST/$name"
    else
        rm -f "$DEST/$name"
        sed "s#%h/projects/auto-library#$REPO#g" "$f" > "$DEST/$name"
    fi
done
systemctl --user daemon-reload
for t in "$HERE"/*.timer; do
    systemctl --user enable --now "$(basename "$t")"
done
# long-running services (no timer): anything with an [Install] section
for s in "$HERE"/*.service; do
    grep -q '^WantedBy=default.target' "$s" && systemctl --user enable --now "$(basename "$s")"
done
systemctl --user list-timers --all | grep -E 'auto-library|discovery|recommend|lyrics|pitchfork|dedup|NEXT'
systemctl --user --no-pager status music-replies.service | head -3
# Timers only fire while you are logged in unless lingering is on (headless servers):
loginctl show-user "$USER" -p Linger 2>/dev/null | grep -q yes \
    || echo "note: run 'sudo loginctl enable-linger $USER' so the timers run when you are logged out"

#!/bin/bash
# Symlink every unit in this directory into the user systemd instance and enable the timers.
# Safe to re-run. Run as the normal user (no sudo): ./systemd/install.sh
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST="$HOME/.config/systemd/user"
mkdir -p "$DEST"
for f in "$HERE"/*.service "$HERE"/*.timer; do
    name="$(basename "$f")"
    # replace plain copies with symlinks so the repo is the single source of truth
    if [ -e "$DEST/$name" ] && [ ! -L "$DEST/$name" ]; then
        mv "$DEST/$name" "$DEST/$name.pre-$(date +%Y%m%d)"
    fi
    ln -sfn "$f" "$DEST/$name"
done
systemctl --user daemon-reload
for t in "$HERE"/*.timer; do
    systemctl --user enable --now "$(basename "$t")"
done
systemctl --user list-timers --all | grep -E 'tidal|discovery|recommend|lyrics|pitchfork|dedup|NEXT'

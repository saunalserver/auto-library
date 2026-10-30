#!/bin/bash
# Auto Library — first-time setup. Checks prerequisites, writes .env, installs
# Python deps, patches tiddl. Safe to re-run; asks before overwriting .env.
set -uo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; BLUE='\033[0;34m'; NC='\033[0m'
ok()   { echo -e "  ${GREEN}✓${NC} $*"; }
warn() { echo -e "  ${YELLOW}!${NC} $*"; }

echo -e "${BLUE}== Auto Library setup ==${NC}"

echo "Prerequisites:"
for cmd in python3 docker fpcalc ffmpeg flac; do
    if command -v "$cmd" >/dev/null; then ok "$cmd"; else warn "$cmd not found"; fi
done
TIDDL_BIN="${TIDDL_BINARY:-$HOME/.local/bin/tiddl}"
if [ -x "$TIDDL_BIN" ]; then ok "tiddl ($TIDDL_BIN)"; else warn "tiddl not found — pipx install tiddl"; fi

ask() {  # ask VAR "prompt" [default]
    local var="$1" prompt="$2" def="${3:-}" val
    while true; do
        read -r -p "  $prompt${def:+ [$def]}: " val
        val="${val:-$def}"
        [ -n "$val" ] && break
        echo "  (required)"
    done
    printf -v "$var" '%s' "$val"
}

write_env=y
if [ -f .env ]; then
    read -r -p ".env already exists. Overwrite it? (y/N): " write_env
fi
if [[ "$write_env" =~ ^[Yy]$ ]]; then
    echo -e "\nLast.fm (https://www.last.fm/api/accounts/create):"
    ask lastfm_key "API key"
    ask lastfm_user "Username whose scrobbles to watch"
    echo -e "\nNavidrome:"
    ask sub_url "URL" "http://localhost:4534"
    ask sub_user "User"
    read -r -s -p "  Password: " sub_pass; echo
    echo -e "\nLibrary:"
    ask music_root "Music folder (absolute path)"
    [ -d "$music_root" ] || warn "$music_root does not exist yet"
    read -r -p "  Mountpoint of the drive it is on (empty = not a separate drive): " lib_mount
    echo -e "\nNotifications:"
    ask ntfy_url "ntfy topic URL" "https://ntfy.sh/auto-library-$RANDOM$RANDOM"
    read -r -s -p "  ntfy token (empty if the topic needs none): " ntfy_token; echo

    umask 077
    cat > .env <<EOF
LASTFM_API_KEY=$lastfm_key
LASTFM_USERNAME=$lastfm_user
SUBSONIC_URL=$sub_url
SUBSONIC_USER=$sub_user
SUBSONIC_PASS=$sub_pass
MUSIC_ROOT=$music_root
LIBRARY_MOUNT=$lib_mount
NTFY_URL=$ntfy_url
NTFY_TOKEN=$ntfy_token
EOF
    ok ".env written (mode 600) — see .env.example for the other settings"
fi

echo
read -r -p "Install Python dependencies (pip install --user -r requirements.txt)? (y/N): " deps
if [[ "$deps" =~ ^[Yy]$ ]]; then
    python3 -m pip install --user -r requirements.txt || warn "pip failed — install python-dotenv and mutagen another way"
fi

echo
python3 scripts/patch_tiddl.py || warn "tiddl patch not applied (see README → Setup)"

echo -e "\n${BLUE}Next:${NC}
  1. tiddl auth login                  # once; the automations refresh the token
  2. python3 monitor.py                # one run by hand, watch the log
  3. ./systemd/install.sh              # enable the timers"

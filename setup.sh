#!/bin/bash

# Tidal Pipeline - Interactive Setup Script

# Colors for better UI
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo -e "${BLUE}=======================================${NC}"
echo -e "${GREEN}   Tidal Pipeline - Setup Wizard       ${NC}"
echo -e "${BLUE}=======================================${NC}"
echo ""

# Prerequisite check
echo -e "${YELLOW}Checking prerequisites...${NC}"
if ! command -v python3 &> /dev/null; then
    echo -e "${YELLOW}Warning: 'python3' not found. Please install Python 3 to run this project.${NC}"
fi
if ! command -v pip &> /dev/null; then
    echo -e "${YELLOW}Warning: 'pip' not found. Please install pip to run this project.${NC}"
fi
echo "Done."
echo ""

# Configuration gathering
echo -e "${BLUE}--- Configuration ---${NC}"

# Last.fm API Key
read -p "Enter your Last.fm API Key: " lastfm_key
while [ -z "$lastfm_key" ]; do
    read -p "API Key cannot be empty. Enter your Last.fm API Key: " lastfm_key
done

# Last.fm Username
read -p "Enter your Last.fm Username: " lastfm_user
while [ -z "$lastfm_user" ]; do
    read -p "Username cannot be empty. Enter your Last.fm Username: " lastfm_user
done

# Music Root
read -p "Enter the absolute path for your Music library [e.g., /mnt/music]: " music_root
while [ -z "$music_root" ]; do
    read -p "Music Root cannot be empty. Enter the path: " music_root
done

if [ ! -d "$music_root" ]; then
    echo -e "${YELLOW}Directory $music_root does not exist.${NC}"
    read -p "Would you like to create it now? (y/n): " create_dir
    if [[ "$docker_choice" =~ ^[Yy]$ ]]; then
        mkdir -p "$music_root"
        echo -e "${GREEN}Created $music_root${NC}"
    fi
fi

# Tiddl Binary
default_tiddl="$HOME/.local/bin/tiddl"
read -p "Enter the path to your 'tiddl' binary [default: $default_tiddl]: " tiddl_bin
tiddl_bin=${tiddl_bin:-$default_tiddl}

# Ntfy URL
read -p "Optional: Enter your Ntfy URL for notifications: " ntfy_url

# Generate .env file
echo ""
echo -e "${YELLOW}Generating .env file...${NC}"
cat > .env << EOF
LASTFM_API_KEY=$lastfm_key
LASTFM_USERNAME=$lastfm_user
MUSIC_ROOT=$music_root
TIDDL_BINARY=$tiddl_bin
NTFY_URL=$ntfy_url
EOF

echo -e "${GREEN}Configuration saved to .env!${NC}"
echo ""

# Installation
read -p "Would you like to install Python dependencies now? (y/n): " install_choice
if [[ "$install_choice" =~ ^[Yy]$ ]]; then
    echo -e "${YELLOW}Installing dependencies...${NC}"
    pip install -r requirements.txt
    echo -e "${GREEN}Dependencies installed successfully!${NC}"
fi

echo ""
echo -e "${BLUE}=======================================${NC}"
echo -e "${GREEN}   Setup Complete!                     ${NC}"
echo -e "${BLUE}=======================================${NC}"
echo ""
echo -e "To start the monitor:"
echo -e "Run manually: ${BLUE}python3 monitor.py${NC}"
echo -e "Or deploy as a service using the provided ${BLUE}tidal-monitor.service${NC} file."
echo ""

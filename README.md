# tidal-monitor

> Automated Tidal music download based on Last.fm listening history

## Quick Start

```bash
# Clone
git clone https://github.com/saunalserver/tidal-monitor.git
cd tidal-monitor

# Install dependencies
pip install -r requirements.txt

# Run monitor
python3 monitor.py
```

## What It Does

Monitors Last.fm scrobbles and automatically downloads full albums from Tidal when you've listened to 3+ tracks from that album.

## Documentation

Full context: `/home/saunalserver/obsidian-vault/nexus/01_PROJECTS/tidal-monitor/CONTEXT.md`

## Components

| Component | Purpose |
|-----------|---------|
| monitor.py | Main script - checks Last.fm, triggers downloads |
| smart_download.py | Validates artist/album match, downloads by ID |
| auto-lyrics.sh | Cron job - fetches missing lyrics |
| auto-recommendations.sh | Cron job - generates recommendations |

## Status

| Field | Value |
|-------|-------|
| State | Active |
| Music folder | `/mnt/photos/flac_music` |
| Last.fm user | `Shlaghetto` |

---

🤖 Generated with [Claude Code](https://claude.com/claude-code)

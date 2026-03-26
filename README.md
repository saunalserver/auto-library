# Tidal Pipeline (Tidal Auto-Monitor)

An intelligent background automation service that bridges your listening habits with local high-fidelity music management. It monitors your Last.fm scrobbles and automatically sources high-quality albums from Tidal when tracks hit your personal popularity thresholds.

## ✨ Key Features
- **Taste-Driven Automation**: Tracks track play counts via Last.fm API. Once a threshold is met, the system identifies the associated album for download.
- **Smart Sourcing**: Utilizes the Tidal API to find the best matching high-quality albums, prioritizing FLAC/high-fidelity versions.
- **Library Integration**: Automatically updates local Navidrome libraries, ensuring your self-hosted music collection stays perfectly in sync with your streaming habits.
- **Robust Error Handling**: Features failed-download tracking with automatic retry logic and authentication expiry monitoring.
- **Headless Architecture**: Designed to run as a background systemd service with comprehensive logging and Ntfy notification support.

## 🛠️ Tech Stack
- **Language**: Python
- **APIs**: Last.fm API, Tidal API
- **Tools**: `tiddl` (Tidal downloader), `navidrome` (Library indexing)
- **Storage**: SQLite (Track play counts and download history)
- **Service**: Systemd

## 🚀 Setup
1. **Clone the repository**
2. **Install dependencies**:
   ```bash
   pip install -r requirements.txt
   ```
3. **Configure Environment**: Copy `.env.example` to `.env` and provide your:
   - `LASTFM_API_KEY` & `LASTFM_USERNAME`
   - `MUSIC_ROOT` (Target download directory)
   - `TIDDL_BINARY` path
4. **Initialize Service**: Deploy the provided `tidal-monitor.service` to your systemd configuration.

## 📊 How it Works
1. **Scrobble Tracking**: The monitor polls Last.fm for recent activity.
2. **Threshold Check**: Track play counts are updated in a local SQLite database.
3. **Smart Download**: When a track hits the threshold (default: 3 plays), the `smart_download.py` script searches Tidal for the most accurate album match.
4. **Library Sync**: Successful downloads are recorded, preventing duplicates and readying the files for Navidrome indexing.

## 🛡️ License
MIT

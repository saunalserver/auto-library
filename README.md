# Tidal Auto-Monitor

Personal music automation for a Navidrome library. Listens to Last.fm, downloads
what you actually play from Tidal (via `tiddl`), keeps the library tidy, and
builds weekly playlists.

Everything lives in `/home/saunalserver/projects/tidal_auto_monitor` and runs as
**user-level systemd timers** (no sudo). Unit files are in `systemd/` and
symlinked into `~/.config/systemd/user/` by `systemd/install.sh`.

## What runs

| Timer | When | Script | Does |
|---|---|---|---|
| `tidal-monitor` | every 6 h (00:10 06:10 12:10 18:10) | `monitor.py` | Last.fm scrobbles → any track played 3× gets its album downloaded. Retries failures, watches singles for full albums, alerts on auth problems. |
| `discovery` | Sun 06:00 | `automations/discovery_recommendations.py` | Up to 10 albums/week from your 7-day top artists and Last.fm "similar artists". |
| `recommendations` | Sun 07:00 | `automations/weekly_playlist.py` | Navidrome playlist **Weekly Discoveries**: 25 unplayed tracks by artists similar to what you played this week. |
| `pitchfork-selects` | daily 07:30 | `automations/pitchfork_selects.py` | Finds the week's *Pitchfork Selects* article (RSS), downloads missing albums, builds playlist **Pitchfork Selects YYYY-MM-DD**. Each article is processed once. |
| `lyrics` | daily 03:00 | `automations/fetch_lyrics.py` | Fetches `.lrc` sidecars from LRCLIB for tracks without lyrics, newest first (400/run). Remembers misses. |
| `dedup-scan` | Mon 04:00 | `dedup_tool.py scan` | Fingerprints the library (`fpcalc`) and records audio-identical duplicates for review. Never deletes anything by itself. |

All scripts share `musiclib.py` (config, rotating logs, ntfy, Subsonic API with
token auth, Navidrome DB access, Tidal token refresh, and the **music-drive
guard**: every automation aborts loudly if `/mnt/photos` is not mounted or
readable instead of downloading into a dead mount).

## Day to day

```bash
systemctl --user list-timers                     # what runs next
journalctl --user -u tidal-monitor -n 50         # logs (also in logs/*.log, rotated)
systemctl --user start tidal-monitor.service     # run one now
python3 monitor.py                               # or run directly

python3 automations/discovery_recommendations.py --dry-run
python3 automations/weekly_playlist.py --dry-run
python3 automations/pitchfork_selects.py --dry-run   # add --force to redo this week
python3 automations/fetch_lyrics.py --limit 20 --dry-run

python3 dedup_tool.py report          # pending duplicate pairs
python3 dedup_tool.py trash <id>      # move one copy to ~/music-trash (reversible)
python3 dedup_tool.py restore <path>
python3 dedup_tool.py purge --older-than 30d --yes
```

State is in `database/monitor.db` (SQLite, WAL): play counts, downloaded
albums, failed downloads and retries, album watch list, lyrics attempts,
fingerprints and dedup findings.

Notifications go to ntfy topic `music` (dedup summary to `music-dedup`).

## Setup

1. `pip install -r requirements.txt` (system Python is fine; `fpcalc`, `ffmpeg`,
   `docker` and `tiddl` (pipx) must be on the PATH).
2. `cp .env.example .env` and fill in Last.fm and Navidrome credentials.
3. `tiddl auth login` once; the automations refresh the token themselves.
4. `./systemd/install.sh`
5. `python3 -m pytest tests` (56 tests, no network needed except one Tidal auth check).

## Gotchas

- `tiddl` exits 0 even when nothing was written (e.g. drive offline). Every
  downloader therefore counts files on disk before recording a success.
- `tiddl`'s `exceptions.py` needs a local `**kwargs` patch (upstream #351);
  `monitor.py` verifies it every run and alerts if a pipx upgrade removed it.
- Tidal titles differ from Last.fm's (singles, deluxe editions, casing), so
  folder lookups are case-insensitive and fall back to the matched Tidal title.
- The library is on a USB drive that has dropped off the bus before. When that
  happens the automations stop and send one ntfy alert per 6 h.

MIT

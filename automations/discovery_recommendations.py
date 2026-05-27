#!/usr/bin/env python3
"""
Discovery Recommendations - Downloads albums based on weekly listening habits.

Improved version that uses smart_download.py for reliable album matching
instead of blind tiddl search which often grabs wrong albums.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sqlite3
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Set, Tuple

LASTFM_API_URL = "http://ws.audioscrobbler.com/2.0/"
NTFY_URL = "http://localhost:8093/music"


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def album_key(artist: str, album: str) -> Tuple[str, str]:
    return normalize(artist), normalize(album)


def notify(title: str, message: str, tags: str = "musical_note", priority: str = "default"):
    """Send notification via ntfy."""
    try:
        data = message.encode("utf-8")
        req = urllib.request.Request(NTFY_URL, data=data, method="POST")
        req.add_header("Title", title)
        req.add_header("Tags", tags)
        req.add_header("Priority", priority)
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        # Log but don't fail the whole script
        import sys
        print(f"Ntfy notification failed: {e}", file=sys.stderr)


@dataclass
class Candidate:
    artist: str
    album: str
    source: str  # "top" or "similar"
    via: Optional[str] = None


class RateLimiter:
    def __init__(self, per_second: float):
        self.interval = 1.0 / per_second if per_second > 0 else 0
        self.last_call = 0.0

    def wait(self) -> None:
        if self.interval <= 0:
            return
        now = time.time()
        elapsed = now - self.last_call
        if elapsed < self.interval:
            time.sleep(self.interval - elapsed)
        self.last_call = time.time()


class LastfmClient:
    def __init__(self, api_key: str, user: str, rate_limit: float, logger: logging.Logger):
        self.api_key = api_key
        self.user = user
        self.logger = logger
        self.rate_limiter = RateLimiter(rate_limit)

    def _call(self, params: dict) -> Optional[dict]:
        merged = {"api_key": self.api_key, "format": "json"}
        merged.update(params)
        query = urllib.parse.urlencode(merged)
        url = f"{LASTFM_API_URL}?{query}"
        self.rate_limiter.wait()
        req = urllib.request.Request(url, headers={"User-Agent": "discovery-recommendations"})
        try:
            with urllib.request.urlopen(req, timeout=25) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            return data
        except Exception as exc:
            self.logger.warning("Last.fm call failed (%s): %s", params.get("method", ""), exc)
            return None

    def get_top_artists(self, limit: int = 10, period: str = "7day") -> List[str]:
        data = self._call({
            "method": "user.gettopartists",
            "user": self.user,
            "limit": str(limit),
            "period": period,
        })
        artists: List[str] = []
        if data:
            items = data.get("topartists", {}).get("artist", [])
            if isinstance(items, dict):
                items = [items]
            for item in items:
                name = item.get("name") if isinstance(item, dict) else None
                if name:
                    artists.append(name)
        self.logger.info("Top artists returned: %d", len(artists))
        return artists

    def get_top_albums(self, artist: str, limit: int = 10) -> List[str]:
        data = self._call({
            "method": "artist.gettopalbums",
            "artist": artist,
            "limit": str(limit),
        })
        albums: List[str] = []
        if data:
            items = data.get("topalbums", {}).get("album", [])
            if isinstance(items, dict):
                items = [items]
            for item in items:
                name = item.get("name") if isinstance(item, dict) else None
                if name:
                    albums.append(name)
        return albums

    def get_similar_artists(self, artist: str, limit: int = 5) -> List[str]:
        data = self._call({
            "method": "artist.getsimilar",
            "artist": artist,
            "limit": str(limit),
        })
        artists: List[str] = []
        if data:
            items = data.get("similarartists", {}).get("artist", [])
            if isinstance(items, dict):
                items = [items]
            for item in items:
                name = item.get("name") if isinstance(item, dict) else None
                if name:
                    artists.append(name)
        return artists


class OwnershipChecker:
    def __init__(self, music_root: Path, monitor_db: Path, navidrome_container: str, navidrome_db: str, logger: logging.Logger):
        self.music_root = music_root
        self.monitor_db = monitor_db
        self.navidrome_container = navidrome_container
        self.navidrome_db = navidrome_db
        self.logger = logger
        self.library_artists: Set[str] = set()
        self.library_albums: Set[Tuple[str, str]] = set()
        self.downloaded: Set[Tuple[str, str]] = set()

    def load(self) -> None:
        self._load_library_from_navidrome()
        self._load_download_history()

    def _load_library_from_navidrome(self) -> None:
        cmd = [
            "docker",
            "exec",
            self.navidrome_container,
            "sqlite3",
            self.navidrome_db,
            "SELECT lower(artist) || '|' || lower(album) FROM media_file GROUP BY lower(artist), lower(album);",
        ]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=40)
            if result.returncode == 0:
                for line in result.stdout.splitlines():
                    if "|" not in line:
                        continue
                    artist, album = line.split("|", 1)
                    a_norm, b_norm = normalize(artist), normalize(album)
                    self.library_artists.add(a_norm)
                    self.library_albums.add((a_norm, b_norm))
                self.logger.info("Library index: %d artists, %d albums (Navidrome)", len(self.library_artists), len(self.library_albums))
            else:
                err = result.stderr.strip()
                self.logger.warning("Navidrome index unavailable (exit %s): %s", result.returncode, err)
        except FileNotFoundError:
            self.logger.warning("Docker not available; skipping Navidrome library check")
        except Exception as exc:
            self.logger.warning("Navidrome index failed: %s", exc)

    def _load_download_history(self) -> None:
        if not self.monitor_db.exists():
            self.logger.warning("Monitor DB not found at %s", self.monitor_db)
            return
        try:
            conn = sqlite3.connect(self.monitor_db)
            cur = conn.cursor()
            cur.execute("SELECT lower(artist), lower(album) FROM downloaded_albums")
            for artist, album in cur.fetchall():
                self.downloaded.add((normalize(artist), normalize(album)))
            conn.close()
            self.logger.info("Download history: %d albums", len(self.downloaded))
        except Exception as exc:
            self.logger.warning("Could not read download history: %s", exc)

    def artist_in_library(self, artist: str) -> bool:
        return normalize(artist) in self.library_artists

    def is_owned(self, artist: str, album: str) -> Tuple[bool, Optional[str]]:
        key = album_key(artist, album)
        if key in self.downloaded:
            return True, "history"
        if key in self.library_albums:
            return True, "library"
        if self._exists_on_disk(artist, album):
            return True, "disk"
        return False, None

    def _exists_on_disk(self, artist: str, album: str) -> bool:
        artist_norm = normalize(artist)
        album_norm = normalize(album)
        if not self.music_root.exists():
            return False
        try:
            for artist_dir in self.music_root.iterdir():
                if not artist_dir.is_dir():
                    continue
                if normalize(artist_dir.name) != artist_norm:
                    continue
                for album_dir in artist_dir.iterdir():
                    if album_dir.is_dir() and normalize(album_dir.name) == album_norm:
                        return True
            return False
        except Exception:
            return False

    def record_download(self, artist: str, album: str) -> None:
        key = album_key(artist, album)
        self.downloaded.add(key)
        if not self.monitor_db.exists():
            return
        try:
            conn = sqlite3.connect(self.monitor_db)
            cur = conn.cursor()
            cur.execute(
                """
                INSERT OR IGNORE INTO downloaded_albums (artist, album, download_date, file_count)
                VALUES (?, ?, datetime('now'), 0)
                """,
                (artist, album),
            )
            conn.commit()
            conn.close()
        except Exception as exc:
            self.logger.warning("Failed to record download in DB: %s", exc)


def select_candidates(top_candidates: List[Candidate], sim_candidates: List[Candidate], max_downloads: int, blend: float) -> List[Candidate]:
    selection: List[Candidate] = []
    target_top = int(round(max_downloads * blend))
    take_top = min(len(top_candidates), target_top)
    selection.extend(top_candidates[:take_top])
    remaining = max_downloads - len(selection)
    take_sim = min(len(sim_candidates), remaining)
    selection.extend(sim_candidates[:take_sim])

    # Fill any remaining slots from leftover pools
    idx_top = take_top
    idx_sim = take_sim
    while len(selection) < max_downloads and (idx_top < len(top_candidates) or idx_sim < len(sim_candidates)):
        if idx_top < len(top_candidates):
            selection.append(top_candidates[idx_top])
            idx_top += 1
            continue
        if idx_sim < len(sim_candidates):
            selection.append(sim_candidates[idx_sim])
            idx_sim += 1
    return selection[:max_downloads]


def write_queue(queue_file: Path, selection: List[Candidate], logger: logging.Logger) -> None:
    try:
        queue_file.parent.mkdir(parents=True, exist_ok=True)
        with open(queue_file, "w", encoding="utf-8") as f:
            for c in selection:
                if c.source == "similar" and c.via:
                    f.write(f"[SIMILAR] {c.artist} - {c.album} (via {c.via})\n")
                else:
                    f.write(f"[TOP] {c.artist} - {c.album}\n")
        logger.info("Wrote queue/report to %s", queue_file)
    except Exception as exc:
        logger.warning("Failed to write queue file: %s", exc)


def run_downloads(selection: List[Candidate], smart_download_path: Path, ownership: OwnershipChecker, logger: logging.Logger) -> Tuple[int, int, List[str], List[str]]:
    """
    Download albums using smart_download.py for reliable matching.
    This avoids the 'tiddl search ... download' anti-pattern that grabs wrong albums.
    Returns (success_count, failure_count, succeeded_names, failed_names).
    """
    if not selection:
        return 0, 0, [], []
    if not smart_download_path.exists():
        logger.error("smart_download.py not found at %s", smart_download_path)
        return 0, len(selection), [], [f"{c.artist} - {c.album}" for c in selection]

    success = 0
    failure = 0
    succeeded = []
    failed = []

    for item in selection:
        logger.info("Smart downloading: %s - %s", item.artist, item.album)

        # Use smart_download.py which validates artist/album match before downloading
        cmd = ["python3", str(smart_download_path), item.artist, item.album, "2"]

        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
            combined_output = result.stdout + result.stderr

            # Log key output
            for line in combined_output.split('\n'):
                if line.strip() and ('Candidate' in line or 'Best match' in line or 'ERROR' in line or 'Downloading' in line):
                    logger.info("  %s", line.strip())

            if result.returncode == 0 and 'Best match' in combined_output:
                logger.info("SUCCESS: %s - %s", item.artist, item.album)
                ownership.record_download(item.artist, item.album)
                success += 1
                succeeded.append(f"{item.artist} - {item.album}")
            elif 'could not find matching album' in combined_output.lower():
                logger.warning("NOT FOUND on Tidal: %s - %s", item.artist, item.album)
                failed.append(f"{item.artist} - {item.album} (not on Tidal)")
                failure += 1
            elif 'auth expired' in combined_output.lower() or 'api_error' in combined_output.lower():
                logger.error("AUTH FAILED: %s - %s", item.artist, item.album)
                failed.append(f"{item.artist} - {item.album} (auth expired)")
                failure += 1
            else:
                logger.error("FAILED: %s - %s (exit %s)", item.artist, item.album, result.returncode)
                failed.append(f"{item.artist} - {item.album} (error)")
                failure += 1
        except Exception as exc:
            logger.error("Exception downloading %s - %s: %s", item.artist, item.album, exc)
            failed.append(f"{item.artist} - {item.album} (exception)")
            failure += 1

        time.sleep(3)

    return success, failure, succeeded, failed


def build_candidates(lastfm: LastfmClient, ownership: OwnershipChecker, top_artists: List[str], logger: logging.Logger) -> Tuple[List[Candidate], List[Candidate], dict]:
    top_candidates: List[Candidate] = []
    sim_candidates: List[Candidate] = []
    seen: Set[Tuple[str, str]] = set()
    skip_stats = {"history": 0, "library": 0, "disk": 0, "artist_filtered": 0}

    logger.info("=== Checking albums from top artists ===")
    for artist in top_artists:
        logger.info("Top artist: %s", artist)
        for album in lastfm.get_top_albums(artist, limit=10):
            if not album:
                continue
            key = album_key(artist, album)
            if key in seen:
                continue
            owned, reason = ownership.is_owned(artist, album)
            if owned:
                if reason in skip_stats:
                    skip_stats[reason] += 1
                logger.info("Skip (owned via %s): %s - %s", reason, artist, album)
                continue
            top_candidates.append(Candidate(artist=artist, album=album, source="top"))
            seen.add(key)

    logger.info("=== Checking similar artists ===")
    for artist in top_artists:
        logger.info("Similar to: %s", artist)
        similar_artists = lastfm.get_similar_artists(artist, limit=5)
        for sim_artist in similar_artists:
            if ownership.artist_in_library(sim_artist):
                skip_stats["artist_filtered"] += 1
                logger.info("Skip similar artist already in library: %s", sim_artist)
                continue
            top_albums = lastfm.get_top_albums(sim_artist, limit=3)
            for album in top_albums:
                if not album:
                    continue
                key = album_key(sim_artist, album)
                if key in seen:
                    continue
                owned, reason = ownership.is_owned(sim_artist, album)
                if owned:
                    if reason in skip_stats:
                        skip_stats[reason] += 1
                    logger.info("Skip (owned via %s): %s - %s", reason, sim_artist, album)
                    continue
                sim_candidates.append(Candidate(artist=sim_artist, album=album, source="similar", via=artist))
                seen.add(key)
                break  # only need one album per similar artist
    return top_candidates, sim_candidates, skip_stats


def setup_logger(log_file: Path) -> logging.Logger:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("discovery")
    logger.handlers.clear()
    logger.setLevel(logging.INFO)
    formatter = logging.Formatter("[%(asctime)s] %(levelname)s: %(message)s")
    fh = logging.FileHandler(log_file)
    fh.setFormatter(formatter)
    logger.addHandler(fh)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(formatter)
    logger.addHandler(sh)
    return logger


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Discovery recommendations")
    parser.add_argument("--dry-run", action="store_true", help="Only report selections")
    parser.add_argument("--report-only", action="store_true", help="Generate report without downloading")
    parser.add_argument("--max", dest="max_downloads", type=int, help="Override max downloads")
    parser.add_argument("--blend", dest="blend", type=float, help="Override top-artist blend ratio (0..1)")
    return parser.parse_args()


def load_config(args: argparse.Namespace) -> dict:
    project_root = Path(__file__).resolve().parent.parent
    env = os.environ
    config = {
        "lastfm_api_key": env.get("LASTFM_API_KEY", "***REMOVED:LASTFM_API_KEY***"),
        "lastfm_user": env.get("LASTFM_USER", "Shlaghetto"),
        "rate_limit": float(env.get("LASTFM_RATE_LIMIT", "1.0")),
        "music_root": Path(env.get("MUSIC_ROOT", "/mnt/photos/flac_music")),
        "monitor_db": Path(env.get("MONITOR_DB", str(project_root / "database" / "monitor.db"))),
        "navidrome_container": env.get("NAVIDROME_CONTAINER", "navidrome"),
        "navidrome_db": env.get("NAVIDROME_DB", "/data/navidrome.db"),
        "smart_download": project_root / "smart_download.py",
        "log_file": Path(env.get("DISCOVERY_LOG", str(project_root / "logs" / "discovery.log"))),
        "queue_file": Path(env.get("DISCOVERY_QUEUE", str(project_root / "discovery-queue.txt"))),
        "max_downloads": args.max_downloads if args.max_downloads is not None else int(env.get("MAX_DOWNLOADS", "10")),
        "blend": args.blend if args.blend is not None else float(env.get("DISCOVERY_BLEND", "0.5")),
        "dry_run": bool(args.dry_run),
        "report_only": bool(args.report_only),
    }
    # Clamp blend
    if config["blend"] < 0:
        config["blend"] = 0.0
    if config["blend"] > 1:
        config["blend"] = 1.0
    return config


def main() -> int:
    args = parse_args()
    config = load_config(args)
    logger = setup_logger(config["log_file"])
    logger.info("========== Discovery Recommendations Started ==========")
    logger.info(
        "Config: user=%s, max=%s, blend=%.2f, dry_run=%s, report_only=%s",
        config["lastfm_user"],
        config["max_downloads"],
        config["blend"],
        config["dry_run"],
        config["report_only"],
    )

    if not config["lastfm_api_key"]:
        logger.error("No LASTFM_API_KEY provided")
        return 1

    ownership = OwnershipChecker(
        music_root=config["music_root"],
        monitor_db=config["monitor_db"],
        navidrome_container=config["navidrome_container"],
        navidrome_db=config["navidrome_db"],
        logger=logger,
    )
    ownership.load()

    lastfm = LastfmClient(config["lastfm_api_key"], config["lastfm_user"], config["rate_limit"], logger)
    top_artists = lastfm.get_top_artists(limit=10, period="7day")
    if not top_artists:
        logger.warning("No top artists returned; nothing to do")
        return 0

    top_candidates, sim_candidates, skip_stats = build_candidates(lastfm, ownership, top_artists, logger)
    logger.info(
        "Candidate pool: %d top-artist, %d similar (skips: history=%d, library=%d, disk=%d, artist_filtered=%d)",
        len(top_candidates),
        len(sim_candidates),
        skip_stats.get("history", 0),
        skip_stats.get("library", 0),
        skip_stats.get("disk", 0),
        skip_stats.get("artist_filtered", 0),
    )

    selection = select_candidates(top_candidates, sim_candidates, config["max_downloads"], config["blend"])
    logger.info("Selected %d albums (target max %d)", len(selection), config["max_downloads"])
    write_queue(config["queue_file"], selection, logger)

    if not selection:
        logger.info("No albums selected after filtering; exiting")
        notify("Discovery Report", "No new albums to download this week", "mag", "low")
        return 0

    if config["dry_run"] or config["report_only"]:
        logger.info("Dry-run/report mode; no downloads will be attempted")
        notify("Discovery Report", f"Found {len(selection)} candidate albums (dry-run)", "mag")
        return 0

    success, failure, succeeded, failed = run_downloads(selection, config["smart_download"], ownership, logger)
    logger.info("Downloads complete: %d success, %d failed", success, failure)

    # Notify results with album names
    if success > 0:
        msg = f"Downloaded {success} new albums:\n" + "\n".join(f"  + {name}" for name in succeeded)
        notify("Discovery Downloads", msg, "headphones,arrow_down")
    if failure > 0:
        msg = f"{failure} albums failed:\n" + "\n".join(f"  x {name}" for name in failed)
        notify("Discovery Failures", msg, "warning", "high")

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("Interrupted", file=sys.stderr)
        sys.exit(1)

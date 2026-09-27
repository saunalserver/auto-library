#!/usr/bin/env python3
"""
Listens on ntfy `music` for your replies to the weekly discovery list.

Everything the automations publish has a title; a message *without* one is
something you typed in the app. Reply with the numbers you want from the list
("1,3,5", "2-4", "all", "none") and the picked albums are downloaded with the
same shared lock as the other downloaders, then Navidrome is rescanned.
Runs as the long-lived user service music-replies.service.
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import discovery_picks as dp  # noqa: E402
import library_cleanup  # noqa: E402
import musiclib as m  # noqa: E402

SINCE_KEY = "ntfy_music_since"
STALE_AFTER = 24 * 3600      # a reply typed while this service was down still counts, for a day
HELP = "Reply with numbers from the discovery list, e.g. 1,3,5 — or 'all' / 'none'."

log = m.setup_logger("music-replies", "music_replies.log")


def reply(title: str, message: str, priority: str = "default") -> None:
    m.notify(title, message, "speech_balloon", priority, logger=log)


def handle(text: str) -> None:
    log.info("Reply received: %r", text)
    conn = m.db_connect()
    suggestions, created = dp.load(conn)
    conn.close()
    if not suggestions or time.time() - created > dp.MAX_AGE:
        reply("Discovery", "There is no open discovery list right now — the next one comes Sunday morning.")
        return
    nums = dp.parse_reply(text, len(suggestions))
    if nums is None:
        reply("Discovery", f"Didn't understand “{text.strip()[:60]}”. {HELP}")
        return
    if not nums:
        reply("Discovery", "OK — nothing from this week's list. Those albums won't be suggested again.")
        return

    chosen = [s for s in suggestions if s.n in nums and s.status == "open"]
    repeats = [s for s in suggestions if s.n in nums and s.status != "open"]
    if not chosen:
        reply("Discovery", "Those are already done: " + ", ".join(f"{s.n}. {s.label()}" for s in repeats))
        return
    reply(f"Downloading {len(chosen)} album{'s' if len(chosen) > 1 else ''}",
          m.fmt_list([f"{s.n}. {s.label()}" for s in chosen], 10, ""))

    if not m.ensure_library(log, "discovery picks"):
        reply("Discovery", "The music drive is not mounted — nothing downloaded. Reply again once it is back.",
              "high")
        return
    m.ensure_tidal_token(log)
    try:
        with dp.download_lock(log):
            done, failed, retired = dp.download_picks(chosen, log)
    except TimeoutError as exc:
        reply("Discovery", f"{exc}. Reply again later.", "high")
        return

    # Statuses are saved so a repeated reply doesn't download twice, and so
    # picked albums don't count as "skipped" next Sunday.
    conn = m.db_connect()
    latest, created = dp.load(conn)
    status = {s.album_id: s.status for s in chosen}
    for s in latest:
        s.status = status.get(s.album_id, s.status)
    dp.save(latest, conn, created=created)
    conn.close()

    if any(s.status == "downloaded" for s in chosen):
        if m.Subsonic(client="music-replies").rescan(log) and retired:
            library_cleanup.repair_playlists(log)
    lines = []
    if done:
        lines += [f"Downloaded {len(done)}:", m.fmt_list(done, 10, "  + ")]
    if failed:
        lines += [f"Failed {len(failed)}:", m.fmt_list(failed, 10, "  x ")]
    if retired:
        lines.append("Removed the single(s) these replace: " + ", ".join(retired))
    m.notify("Discovery picks done", "\n".join(lines), "headphones,arrow_down",
             "high" if failed and not done else "default", logger=log)


def listen() -> None:
    base = m.NTFY_URL.rstrip("/")
    headers = {}
    auth = m._ntfy_auth_header()
    if auth:
        headers["Authorization"] = auth
    log.info("Listening for replies on %s", base)
    while True:
        conn = m.db_connect()
        since = m.get_state(conn, SINCE_KEY) or str(int(time.time()))
        conn.close()
        try:
            req = urllib.request.Request(f"{base}/json?since={since}", headers=headers)
            with urllib.request.urlopen(req, timeout=90) as stream:   # ntfy keepalives every 45 s
                for raw in stream:
                    msg = json.loads(raw)
                    if msg.get("event") != "message":
                        continue
                    conn = m.db_connect()
                    m.set_state(conn, SINCE_KEY, msg["id"])
                    conn.close()
                    if msg.get("title"):
                        continue                                  # an automation's own output
                    if time.time() - msg.get("time", 0) > STALE_AFTER:
                        continue
                    try:
                        handle(msg.get("message", ""))
                    except Exception:  # noqa: BLE001
                        log.exception("Handling reply failed")
                        reply("Discovery", "Something went wrong handling that reply — see logs/music_replies.log.",
                              "high")
        except Exception as exc:  # noqa: BLE001
            log.warning("ntfy stream: %s; reconnecting", exc)
        time.sleep(5)


if __name__ == "__main__":
    try:
        listen()
    except KeyboardInterrupt:
        pass

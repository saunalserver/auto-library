"""Not every audio-identical pair is waste.

Charli XCX's "BRAT", its remix album and its extended edition legitimately
share tracks; the first full library scan flagged 158 pairs and reported
5.8 GB "reclaimable", which would have meant gutting albums. Only two files
inside the same album folder (a clean and an "(Explicit)" rip) are safe.
"""
from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import dedup_tool as dt  # noqa: E402

ROOT = "/mnt/photos/flac_music"


def test_same_album_folder_is_safe_to_reclaim():
    kind = dt.classify_pair(
        f"{ROOT}/Ariana Grande/thank u, next/02 - needy.flac",
        f"{ROOT}/Ariana Grande/thank u, next/02 - needy(Explicit).flac",
    )
    assert kind == dt.SAME_ALBUM


def test_two_releases_by_one_artist_are_not_waste():
    """'360' appears on BRAT and on the remix album. Both albums need it."""
    kind = dt.classify_pair(
        f"{ROOT}/Charli XCX/BRAT/01 - 360.flac",
        f"{ROOT}/Charli XCX/Brat and it's completely different but also still brat/01 - 360.flac",
    )
    assert kind == dt.SAME_ARTIST


def test_different_artist_folders_are_flagged_separately():
    kind = dt.classify_pair(
        f"{ROOT}/Various Artists/Some Compilation/03 - Track.flac",
        f"{ROOT}/Real Artist/Their Album/03 - Track.flac",
    )
    assert kind == dt.CROSS_ARTIST


def test_every_kind_has_guidance_text():
    for kind in (dt.SAME_ALBUM, dt.SAME_ARTIST, dt.CROSS_ARTIST):
        assert dt._KIND_HELP[kind]


def test_summary_counts_only_same_album_as_reclaimable(tmp_path, capsys):
    """The ntfy line must not advertise shared album tracks as free space."""
    db = tmp_path / "d.db"
    conn = dt._connect(str(db))
    rows = [
        # safe: same folder, 1 GB
        (f"{ROOT}/A/Album/01 - t.flac", f"{ROOT}/A/Album/01 - t(Explicit).flac", 10**9),
        # not safe: two releases by one artist, 5 GB — must not be counted
        (f"{ROOT}/A/Album/02 - u.flac", f"{ROOT}/A/Other Album/02 - u.flac", 5 * 10**9),
    ]
    for i, (fp, mp, size) in enumerate(rows):
        for a, b in ((fp, mp), (mp, fp)):
            conn.execute(
                "INSERT INTO dedup_findings (group_id, filepath, artist, album, title,"
                " similarity, matched_path, status, size_bytes, added_at)"
                " VALUES (?,?,?,?,?,?,?,'pending',?,0)",
                (str(i), a, "A", "Album", "t", 1.0, b, size),
            )
    conn.commit()
    conn.close()

    args = type("A", (), {"db": str(db), "ntfy_url": ""})()
    dt.cmd_ntfy_summary(args)
    out = capsys.readouterr().out
    assert "1 safe duplicate" in out
    assert "1.0 GB" in out          # the 5 GB shared track is excluded
    assert "6.0 GB" not in out

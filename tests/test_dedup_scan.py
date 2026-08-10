import sqlite3
import subprocess
from pathlib import Path

import pytest

from dedup_lib import init_dedup_schema
import dedup_scan

@pytest.fixture
def fake_library(tmp_path):
    """Build a tiny library with two duplicate tracks."""
    root = tmp_path / "music"
    # /Artist/Album/track.flac
    paths = [
        root / "JAY-Z" / "Reasonable Doubt" / "01 - Can't Knock the Hustle.flac",
        root / "JAY Z" / "Best Of" / "01 - Can't Knock the Hustle.flac",  # dup, split artist
        root / "Other" / "Album" / "02 - Different.flac",
    ]
    for p in paths:
        p.parent.mkdir(parents=True)
        # 4-second 440Hz sine. NOTE: chromaprint needs >=3s of real audio
        # (silence or pure sines <3s produce empty fingerprints and fpcalc errors).
        subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i",
                        "sine=frequency=440:duration=4", "-t", "4", str(p)],
                       check=True, capture_output=True)
    return root

def test_scan_indexes_all_files(tmp_path, fake_library):
    db = tmp_path / "test.db"
    conn = sqlite3.connect(db)
    init_dedup_schema(conn)
    dedup_scan.scan_library(conn, fake_library)
    cur = conn.execute("SELECT COUNT(*) FROM audio_fingerprints")
    assert cur.fetchone()[0] == 3

def test_scan_extracts_metadata_from_path(tmp_path, fake_library):
    db = tmp_path / "test.db"
    conn = sqlite3.connect(db)
    init_dedup_schema(conn)
    dedup_scan.scan_library(conn, fake_library)
    cur = conn.execute("SELECT artist, album, title FROM audio_fingerprints WHERE filepath LIKE '%/JAY-Z/%'")
    row = cur.fetchone()
    assert row[0] == "JAY-Z"
    assert row[1] == "Reasonable Doubt"
    assert "Can't Knock the Hustle" in row[2]

def test_scan_is_incremental(tmp_path, fake_library):
    db = tmp_path / "test.db"
    conn = sqlite3.connect(db)
    init_dedup_schema(conn)
    dedup_scan.scan_library(conn, fake_library)
    # Re-scan — should not re-fingerprint. Track scanned_at timestamps.
    cur = conn.execute("SELECT scanned_at FROM audio_fingerprints")
    first_times = [r[0] for r in cur.fetchall()]
    dedup_scan.scan_library(conn, fake_library)
    cur = conn.execute("SELECT scanned_at FROM audio_fingerprints")
    second_times = [r[0] for r in cur.fetchall()]
    assert first_times == second_times  # no re-scan if mtime unchanged

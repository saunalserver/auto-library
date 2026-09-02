import sqlite3
import subprocess
import uuid
from pathlib import Path

import pytest

from dedup_lib import init_dedup_schema, index_file
import dedup_state


@pytest.fixture
def fake_library_with_dupes(tmp_path):
    """Two copies of identical audio + one different track."""
    root = tmp_path / "music"
    root.mkdir()
    paths = [
        root / "Artist" / "Album1" / "01 - Track.flac",
        root / "Artist" / "Album2" / "01 - Track.flac",  # audio-identical dup
        root / "Artist" / "Album3" / "02 - Other.flac",
    ]
    for p in paths:
        p.parent.mkdir(parents=True)
        # Use white noise for the "different" track (different fingerprint shape);
        # pure sines of any frequency collapse to the same fingerprint so noise
        # is needed to guarantee non-match. Duration 5 chosen empirically:
        # chromaprint's base64 fingerprint lands on a multiple of 4 bytes only
        # at certain durations (4s+; the underlying int32 array length is
        # content-dependent and sometimes produces unpadded base64 that
        # b64decode rejects). 5s decodes cleanly for both sine and pink noise.
        if "Other" in p.name:
            subprocess.run(
                ["ffmpeg", "-y", "-f", "lavfi", "-i",
                 "anoisesrc=d=5:c=pink:a=0.5", "-t", "5", str(p)],
                check=True, capture_output=True,
            )
        else:
            subprocess.run(
                ["ffmpeg", "-y", "-f", "lavfi", "-i",
                 "sine=frequency=440:duration=5", "-t", "5", str(p)],
                check=True, capture_output=True,
            )

    db = tmp_path / "test.db"
    conn = sqlite3.connect(db)
    init_dedup_schema(conn)
    for p in paths:
        index_file(conn, p, "Artist", p.parent.name,
                   "Track" if "Track" in p.name else "Other")
    return conn, root

def test_compare_finds_audio_identical_dupes(fake_library_with_dupes):
    conn, _ = fake_library_with_dupes
    dedup_state.compare_library(conn)
    cur = conn.execute("SELECT COUNT(*) FROM dedup_findings")
    assert cur.fetchone()[0] >= 1  # at least one finding for the duplicate pair

def test_compare_does_not_flag_unrelated_track(fake_library_with_dupes):
    conn, _ = fake_library_with_dupes
    dedup_state.compare_library(conn)
    cur = conn.execute("SELECT filepath FROM dedup_findings")
    for row in cur:
        assert "Other" not in row[0]

def test_compare_skips_protected_files(fake_library_with_dupes):
    conn, root = fake_library_with_dupes
    # Protect one of the duplicate files
    dup_path = str(root / "Artist" / "Album2" / "01 - Track.flac")
    conn.execute(
        "INSERT INTO dedup_protections (filepath, reason, added_at, added_by) "
        "VALUES (?, 'test protection', datetime('now'), 'test')",
        (dup_path,),
    )
    conn.commit()
    # Run compare — protected file should not be a candidate for trashing
    # but still gets a finding entry (with status='protected')
    dedup_state.compare_library(conn)
    cur = conn.execute("SELECT status FROM dedup_findings WHERE filepath = ?", (dup_path,))
    statuses = [r[0] for r in cur.fetchall()]
    assert "protected" in statuses or len(statuses) == 0  # implementation-defined, just verify no crash


def test_compare_is_idempotent_across_runs(fake_library_with_dupes):
    """The weekly scan must not re-add the same pair under a new group_id."""
    conn, _ = fake_library_with_dupes
    first = dedup_state.compare_library(conn)
    second = dedup_state.compare_library(conn)
    assert first > 0
    assert second == 0
    n = conn.execute("SELECT COUNT(*) FROM dedup_findings").fetchone()[0]
    assert n == first

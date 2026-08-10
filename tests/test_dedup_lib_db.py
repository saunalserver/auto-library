import sqlite3
import tempfile
from pathlib import Path

import pytest

from dedup_lib import init_dedup_schema, index_file, find_existing_fingerprints

@pytest.fixture
def tmpdb():
    with tempfile.NamedTemporaryFile(suffix=".db") as f:
        conn = sqlite3.connect(f.name)
        init_dedup_schema(conn)
        yield conn
        conn.close()

def test_schema_creates_all_tables(tmpdb):
    cur = tmpdb.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
    names = {row[0] for row in cur.fetchall()}
    assert {"audio_fingerprints", "dedup_findings", "dedup_protections", "dedup_log"} <= names

def test_index_file_inserts_row(tmpdb, tmp_path):
    # Create a tiny FLAC and index it. NOTE: a sine wave (not anullsrc silence)
    # is required because chromaprint returns "Empty fingerprint" for pure
    # silence; and the clip must be >=3s for chromaprint to produce output.
    # Same lesson as tests/fixtures/gen_fixtures.py.
    import subprocess
    fp = tmp_path / "test.flac"
    subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i",
                    "sine=frequency=440:duration=3", "-t", "3", str(fp)],
                   check=True, capture_output=True)
    index_file(tmpdb, fp, "Test Artist", "Test Album", "Test Title")
    cur = tmpdb.execute("SELECT COUNT(*) FROM audio_fingerprints")
    assert cur.fetchone()[0] == 1

def test_find_existing_fingerprints_buckets_by_normalized(tmpdb):
    # Manually insert two rows with the same normalized artist/title
    tmpdb.execute(
        "INSERT INTO audio_fingerprints (filepath, artist, album, title, "
        "normalized_artist, normalized_title, duration_ms, fingerprint, "
        "fingerprint_version, file_size, file_mtime, scanned_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        ("/a.flac", "JAY-Z", "Album", "Track", "jay z", "track", 1000, "AAA", 2, 100, 1.0, 0),
    )
    rows = find_existing_fingerprints(tmpdb, "jay z", "track")
    assert len(rows) == 1
    assert rows[0]["filepath"] == "/a.flac"

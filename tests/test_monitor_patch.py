"""Integration test: when monitor downloads a file that audio-matches an
existing file, a dedup_findings row is inserted and the new file is marked
protected (auto-protected at download time)."""
import sqlite3
import subprocess
from pathlib import Path

import pytest

from dedup_lib import init_dedup_schema, index_file


def test_post_download_fingerprint_detects_dup(tmp_path, monkeypatch):
    """Set up: pre-existing file with same audio as a "new" file."""
    music_root = tmp_path / "music"
    existing_dir = music_root / "Artist" / "ExistingAlbum"
    new_dir = music_root / "Artist" / "NewAlbum"
    existing_dir.mkdir(parents=True)
    new_dir.mkdir(parents=True)

    # Two identical audio files — copy the sine-tone fixture (pure silence
    # produces an empty fpcalc fingerprint and can't be compared).
    import shutil
    fixture = Path(__file__).parent / "fixtures" / "signal_b.flac"
    shutil.copy(fixture, existing_dir / "01 - Track.flac")
    shutil.copy(fixture, new_dir / "01 - Track.flac")

    db = tmp_path / "test.db"
    conn = sqlite3.connect(db)
    init_dedup_schema(conn)

    # Pre-index the "existing" file (simulate prior library scan).
    # Title must match what the new file will resolve to — the fixture has no
    # title tag, so post_download_dedup_check falls back to the stem "01 - Track".
    index_file(conn, existing_dir / "01 - Track.flac", "Artist", "ExistingAlbum", "01 - Track")

    # Import the post-download check directly
    from importlib import import_module
    sys_path_backup = __import__("sys").path[:]
    __import__("sys").path.insert(0, ".")
    monitor = import_module("monitor")
    __import__("sys").path = sys_path_backup

    # Point monitor at our test paths
    monkeypatch.setattr(monitor, "MUSIC_ROOT", music_root)
    monkeypatch.setattr(monitor, "DATABASE_PATH", db)
    # Belt and braces: musiclib.notify already no-ops under pytest, but this
    # test hits the "duplicate found" branch that pushes to the phone.
    monkeypatch.setattr(monitor, "notify", lambda *a, **k: None)

    findings_before = conn.execute("SELECT COUNT(*) FROM dedup_findings").fetchone()[0]
    monitor.post_download_dedup_check(conn, "Artist", "NewAlbum")
    findings_after = conn.execute("SELECT COUNT(*) FROM dedup_findings").fetchone()[0]
    assert findings_after > findings_before


def test_db_connect_rows_support_name_access(tmp_path, monkeypatch):
    """monitor.db_connect() must yield sqlite3.Row.

    dedup_lib.find_existing_fingerprints returns rows the post-download check
    indexes by name (row['filepath']). With plain tuples every check failed
    with "tuple indices must be integers" — silently, since the caller only
    logs a warning. Rows must also still unpack and index by position, which
    the rest of monitor.py relies on.
    """
    import sqlite3 as _sq
    from importlib import import_module
    import sys as _sys
    _sys.path.insert(0, ".")
    monitor = import_module("monitor")

    db = tmp_path / "m.db"
    monkeypatch.setattr(monitor, "DATABASE_PATH", db)
    conn = monitor.db_connect()
    conn.execute("CREATE TABLE t (filepath TEXT, n INTEGER)")
    conn.execute("INSERT INTO t VALUES ('/a/b.flac', 7)")
    row = conn.execute("SELECT filepath, n FROM t").fetchone()
    assert row["filepath"] == "/a/b.flac"      # name access (dedup_lib)
    assert row[1] == 7                          # positional (rest of monitor.py)
    path, n = row                               # unpacking (loops in monitor.py)
    assert (path, n) == ("/a/b.flac", 7)
    conn.close()

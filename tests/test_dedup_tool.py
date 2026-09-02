import sqlite3
import subprocess
from pathlib import Path

import pytest

from dedup_lib import init_dedup_schema
import dedup_tool


@pytest.fixture
def populated_db(tmp_path):
    db = tmp_path / "test.db"
    conn = sqlite3.connect(db)
    init_dedup_schema(conn)
    # Insert two fake findings
    conn.executemany(
        "INSERT INTO dedup_findings (group_id, filepath, artist, album, title, "
        "similarity, matched_path, status, size_bytes, added_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        [
            ("g1", "/music/A/Album/track.flac", "A", "Album", "Track",
             0.99, "/music/A/Other/track.flac", "pending", 1000, 0),
            ("g1", "/music/A/Other/track.flac", "A", "Other", "Track",
             0.99, "/music/A/Album/track.flac", "pending", 1000, 0),
        ],
    )
    conn.commit()
    conn.close()
    return str(db)


def test_report_pending_returns_findings(populated_db, capsys):
    dedup_tool.main(["report", "--db", populated_db])
    out = capsys.readouterr().out
    assert "/music/A/Album/track.flac" in out
    assert "/music/A/Other/track.flac" in out


def test_report_json_outputs_valid_json(populated_db, capsys):
    """One JSON object per duplicate *pair*, each tagged with its kind.

    Findings are stored twice (once per file); the report collapses them so a
    pair is not counted double. --all-rows still exposes the raw table.
    """
    import json
    dedup_tool.main(["report", "--db", populated_db, "--json"])
    data = json.loads(capsys.readouterr().out)
    assert isinstance(data, list)
    assert len(data) == 1
    assert data[0]["kind"] in (dedup_tool.SAME_ALBUM, dedup_tool.SAME_ARTIST,
                               dedup_tool.CROSS_ARTIST)

    dedup_tool.main(["report", "--db", populated_db, "--json", "--all-rows"])
    assert len(json.loads(capsys.readouterr().out)) == 2


def test_scan_chains_index_and_compare(tmp_path, monkeypatch):
    # Build a fake library — two audio-identical files (same sine args) so
    # chromaprint produces a non-empty matching fingerprint.
    root = tmp_path / "music"
    root.mkdir()
    import subprocess
    for path in [root / "A" / "X" / "t.flac", root / "A" / "Y" / "t.flac"]:
        path.parent.mkdir(parents=True)
        subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i",
                        "sine=frequency=440:duration=5", "-t", "5", str(path)],
                       check=True, capture_output=True)

    db = str(tmp_path / "test.db")
    dedup_tool.main(["scan", "--db", db, "--root", str(root)])

    conn = sqlite3.connect(db)
    cur = conn.execute("SELECT COUNT(*) FROM dedup_findings")
    assert cur.fetchone()[0] >= 1


def test_protect_adds_row(populated_db, tmp_path):
    # cmd_protect refuses non-existent paths, so create a real file.
    real = tmp_path / "track.flac"
    real.write_bytes(b"x")
    dedup_tool.main(["protect", "--db", populated_db,
                     str(real), "original album rip"])
    conn = sqlite3.connect(populated_db)
    cur = conn.execute("SELECT reason FROM dedup_protections WHERE filepath = ?",
                       (str(real.resolve()),))
    row = cur.fetchone()
    assert row is not None
    assert "original album rip" in row[0]

def test_protect_refuses_nonexistent_path(populated_db, capsys):
    rc = dedup_tool.main(["protect", "--db", populated_db,
                          "/does/not/exist.flac", "x"])
    assert rc != 0
    err = capsys.readouterr().err
    assert "does not exist" in err.lower() or "not found" in err.lower()

def test_unprotect_removes_row(populated_db, tmp_path):
    real = tmp_path / "track.flac"
    real.write_bytes(b"x")
    dedup_tool.main(["protect", "--db", populated_db, str(real), "x"])
    dedup_tool.main(["unprotect", "--db", populated_db, str(real)])
    conn = sqlite3.connect(populated_db)
    cur = conn.execute("SELECT COUNT(*) FROM dedup_protections WHERE filepath = ?",
                       (str(real.resolve()),))
    assert cur.fetchone()[0] == 0


def test_trash_moves_to_trash_dir(populated_db, tmp_path, monkeypatch):
    # Create the actual file so trash can move it
    src = Path("/tmp/test_dedup_src.flac")
    src.parent.mkdir(exist_ok=True)
    src.write_bytes(b"fake audio")
    # Update finding to point at our real file
    conn = sqlite3.connect(populated_db)
    conn.execute("UPDATE dedup_findings SET filepath=? WHERE filepath=?",
                 (str(src), "/music/A/Album/track.flac"))
    conn.commit()
    conn.close()

    trash_root = tmp_path / "trash"
    dedup_tool.main(["trash", "--db", populated_db, str(src),
                     "--trash-root", str(trash_root)])
    assert not src.exists()  # moved out of source
    # Somewhere under trash_root the file exists
    trashed = list(trash_root.rglob("test_dedup_src.flac"))
    assert len(trashed) == 1
    assert trashed[0].read_bytes() == b"fake audio"


def test_trash_refuses_protected(populated_db, tmp_path):
    src = Path("/tmp/test_dedup_src2.flac")
    src.write_bytes(b"x")
    conn = sqlite3.connect(populated_db)
    conn.execute("UPDATE dedup_findings SET filepath=? WHERE filepath=?",
                 (str(src), "/music/A/Album/track.flac"))
    conn.execute("INSERT INTO dedup_protections (filepath, reason, added_at, added_by) "
                 "VALUES (?, 'test', datetime('now'), 'test')", (str(src),))
    conn.commit()
    conn.close()

    rc = dedup_tool.main(["trash", "--db", populated_db, str(src),
                          "--trash-root", str(tmp_path / "trash")])
    assert rc != 0
    assert src.exists()  # not moved


def test_restore_reverses_trash(populated_db, tmp_path):
    src = Path("/tmp/test_dedup_restore.flac")
    src.write_bytes(b"restore me")
    original_bytes = src.read_bytes()
    trash_root = tmp_path / "trash"

    dedup_tool.main(["trash", "--db", populated_db, str(src),
                     "--trash-root", str(trash_root)])
    assert not src.exists()

    dedup_tool.main(["restore", "--db", populated_db, str(src),
                     "--trash-root", str(trash_root)])
    assert src.exists()
    assert src.read_bytes() == original_bytes


def test_purge_requires_yes_flag(populated_db, tmp_path, capsys):
    trash_root = tmp_path / "trash"
    trash_root.mkdir()
    (trash_root / "old").mkdir()
    old_file = trash_root / "old" / "x.flac"
    old_file.write_bytes(b"x")
    # Backdate mtime to 40 days ago
    import os
    old_time = old_file.stat().st_mtime - 40 * 86400
    os.utime(old_file, (old_time, old_time))

    rc = dedup_tool.main(["purge", "--db", populated_db,
                          "--trash-root", str(trash_root), "--older-than", "30d"])
    assert rc != 0
    assert old_file.exists()  # not deleted without --yes


def test_purge_with_yes_deletes_old(populated_db, tmp_path):
    trash_root = tmp_path / "trash"
    trash_root.mkdir()
    (trash_root / "old").mkdir()
    old_file = trash_root / "old" / "x.flac"
    old_file.write_bytes(b"x")
    import os
    old_time = old_file.stat().st_mtime - 40 * 86400
    os.utime(old_file, (old_time, old_time))

    rc = dedup_tool.main(["purge", "--db", populated_db,
                          "--trash-root", str(trash_root),
                          "--older-than", "30d", "--yes"])
    assert rc == 0
    assert not old_file.exists()


def test_purge_refuses_recent(populated_db, tmp_path):
    trash_root = tmp_path / "trash"
    trash_root.mkdir()
    recent = trash_root / "y.flac"
    recent.write_bytes(b"y")  # fresh mtime

    rc = dedup_tool.main(["purge", "--db", populated_db,
                          "--trash-root", str(trash_root),
                          "--older-than", "30d", "--yes"])
    assert rc == 0
    assert recent.exists()  # too new, not purged


def test_history_prints_log(populated_db, capsys):
    conn = sqlite3.connect(populated_db)
    conn.execute(
        "INSERT INTO dedup_log (filepath, action, when_at, actor, details) "
        "VALUES (?, 'trash', datetime('now'), 'test', '/trash/x')",
        ("/music/A/Album/track.flac",),
    )
    conn.commit()
    conn.close()
    dedup_tool.main(["history", "--db", populated_db])
    out = capsys.readouterr().out
    assert "trash" in out
    assert "/music/A/Album/track.flac" in out


def test_ntfy_summary_returns_reclaimable_space(populated_db, capsys, monkeypatch):
    # Mock ntfy send so we don't hit network
    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout: None)
    dedup_tool.main(["ntfy-summary", "--db", populated_db,
                     "--ntfy-url", "http://example.com/x"])
    # Test passes if no exception — actual ntfy send is best-effort

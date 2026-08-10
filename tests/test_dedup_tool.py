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
    import json
    dedup_tool.main(["report", "--db", populated_db, "--json"])
    out = capsys.readouterr().out
    data = json.loads(out)
    assert isinstance(data, list)
    assert len(data) == 2

"""Unit tests for the shared helpers (no network, no docker)."""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import musiclib as m  # noqa: E402


def test_library_available_false_when_root_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(m, "LIBRARY_MOUNT", None)
    monkeypatch.setattr(m, "MUSIC_ROOT", tmp_path / "nope")
    assert m.library_available() is False


def test_library_available_false_when_root_empty(monkeypatch, tmp_path):
    """An empty mountpoint directory is what a dead USB drive looks like."""
    monkeypatch.setattr(m, "LIBRARY_MOUNT", None)
    monkeypatch.setattr(m, "MUSIC_ROOT", tmp_path)
    assert m.library_available() is False


def test_library_available_true_with_content(monkeypatch, tmp_path):
    monkeypatch.setattr(m, "LIBRARY_MOUNT", None)
    (tmp_path / "Artist").mkdir()
    monkeypatch.setattr(m, "MUSIC_ROOT", tmp_path)
    assert m.library_available() is True


def test_library_available_false_when_not_a_mountpoint(monkeypatch, tmp_path):
    (tmp_path / "Artist").mkdir()
    monkeypatch.setattr(m, "LIBRARY_MOUNT", tmp_path)  # a plain dir, not a mount
    monkeypatch.setattr(m, "MUSIC_ROOT", tmp_path)
    assert m.library_available() is False


def test_find_album_dirs_case_insensitive(tmp_path):
    (tmp_path / "Charli XCX" / "Brat").mkdir(parents=True)
    (tmp_path / "Charli xcx" / "BRAT").mkdir(parents=True)
    (tmp_path / "Charli xcx" / "BRAT" / "01 - x.flac").write_bytes(b"x")
    (tmp_path / "Other" / "Brat").mkdir(parents=True)
    dirs = m.find_album_dirs("charli xcx", "brat", root=tmp_path)
    assert len(dirs) == 2
    assert m.count_audio_files("CHARLI XCX", "brat", root=tmp_path) == 1
    assert m.count_audio_files("Charli XCX", "Nope", root=tmp_path) == 0


def test_subsonic_auth_uses_salted_token_not_password():
    sub = m.Subsonic(url="http://x", user="u", password="secret", client="t")
    params = sub._auth_params()
    assert "p" not in params
    assert params["t"] == hashlib.md5(("secret" + params["s"]).encode()).hexdigest()
    assert params["u"] == "u" and params["c"] == "t" and params["f"] == "json"


def test_fmt_list_truncates():
    out = m.fmt_list([str(i) for i in range(12)], max_items=3)
    assert out.count("\n") == 3 and "9 more" in out


def test_ensure_tidal_token_skips_refresh_when_fresh(monkeypatch):
    import time
    monkeypatch.setattr(m, "tidal_token_expiry", lambda: int(time.time()) + 7200)
    monkeypatch.setattr(m, "tidal_refresh_token", lambda logger=None: (_ for _ in ()).throw(AssertionError("should not refresh")))
    assert m.ensure_tidal_token() is True


def test_find_album_dirs_ignores_chars_tiddl_strips(tmp_path):
    (tmp_path / "Turnstile" / "NEVER ENOUGH VERSIONS").mkdir(parents=True)
    (tmp_path / "Turnstile" / "NEVER ENOUGH VERSIONS" / "01 - x.flac").write_bytes(b"x")
    assert m.count_audio_files("Turnstile", "NEVER ENOUGH: VERSIONS", root=tmp_path) == 1
    assert m.count_audio_files("PinkPantheress", "Fancy Some More?", root=tmp_path) == 0


def test_notify_is_suppressed_under_pytest():
    """Tests must never push to the user's phone.

    The dedup integration test exercises the "duplicate downloaded" branch;
    before this guard every `pytest` run sent a real ntfy alert.
    """
    assert m.notify("test", "should not be sent") is False

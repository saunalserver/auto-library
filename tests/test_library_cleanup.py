"""Retiring a single once its album is in the library — with real fingerprints.

Builds a tiny library of generated audio (distinct noise per "song"), so the
test exercises fpcalc and the comparison rather than a mock.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import library_cleanup as lc  # noqa: E402
import musiclib as m  # noqa: E402

pytestmark = pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("fpcalc")),
                                reason="needs ffmpeg + fpcalc")


def song(path: Path, seed: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i",
                    f"anoisesrc=d=20:c=pink:seed={seed}:a=0.5", "-ac", "2", str(path)], check=True)


@pytest.fixture
def library(tmp_path, monkeypatch):
    root = tmp_path / "flac_music"
    monkeypatch.setattr(m, "MUSIC_ROOT", root)
    monkeypatch.setattr(m, "DB_PATH", tmp_path / "monitor.db")
    monkeypatch.setattr(lc, "TRASH_ROOT", tmp_path / "trash")
    album = root / "Artist" / "The Album"
    for n, seed in enumerate((1, 2, 3, 4), 1):
        song(album / f"0{n} - Artist - Song {n}.flac", seed)
    shutil.copy(album / "02 - Artist - Song 2.flac", (root / "Artist" / "Song 2").mkdir(parents=True) or
                root / "Artist" / "Song 2" / "01 - Artist - Song 2.flac")
    song(root / "Artist" / "Song 2 (Remix)" / "01 - Artist - Song 2.flac", 99)   # same title, other audio
    return root, album


def test_single_on_the_album_is_retired_and_a_remix_is_kept(library):
    root, album = library
    retired = lc.retire_superseded_singles([album])
    assert retired == ["Artist - Song 2"]
    assert not (root / "Artist" / "Song 2").exists()
    assert (root / "Artist" / "Song 2 (Remix)").exists()
    assert list(lc.TRASH_ROOT.glob("*/Artist/Song 2/01 - Artist - Song 2.flac"))


def test_restore_puts_it_back(library):
    root, album = library
    lc.retire_superseded_singles([album])
    lc.restore_folder("Artist/Song 2")
    assert (root / "Artist" / "Song 2" / "01 - Artist - Song 2.flac").exists()


def test_a_flac_whose_frames_do_not_decode_is_broken(tmp_path):
    good = tmp_path / "good.flac"
    song(good, 5)
    assert not lc.is_broken(good)
    bad = tmp_path / "bad.flac"
    data = bytearray(good.read_bytes())
    mid = len(data) // 2
    data[mid:mid + 4096] = bytes(4096)          # zero out a run of audio frames
    bad.write_bytes(bytes(data))
    assert lc.is_broken(bad)


def test_broken_files_are_moved_out_for_re_download(library):
    root, album = library
    victim = album / "03 - Artist - Song 3.flac"
    data = bytearray(victim.read_bytes())
    data[len(data) // 2:len(data) // 2 + 4096] = bytes(4096)
    victim.write_bytes(bytes(data))
    assert lc.remove_broken_files([album]) == [victim.name]
    assert not victim.exists()
    assert len(m.audio_files(album)) == 3


def test_old_and_new_tiddl_names_of_one_track_collapse_to_the_new_one(library):
    root, album = library
    old = album / "02 - Artist - Song 2.flac"
    new = album / "1-02 - Artist - Song 2.flac"
    shutil.copy(old, new)
    old.with_suffix(".lrc").write_text("[00:01.00] la\n")
    assert lc.dedupe_renumbered(album) == [old.name]
    assert new.exists() and not old.exists()
    assert new.with_suffix(".lrc").read_text() == "[00:01.00] la\n"      # lyrics carried over
    # different tracks with the same number on different discs are left alone
    shutil.copy(album / "01 - Artist - Song 1.flac", album / "2-02 - Artist - Other Song.flac")
    assert lc.dedupe_renumbered(album) == []

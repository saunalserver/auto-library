"""The monitor's "is this track already here?" check.

Real cases, 2026-09: a daily high-priority "10 albums permanently failed"
push listed only things already in the library — collaborations filed under
the lead artist and an artist's old alias.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import monitor  # noqa: E402


def checker(rows):
    c = monitor.LibraryChecker()
    for artist, album, title in rows:
        names = monitor.artist_names(artist)
        c.library_tracks.setdefault(monitor.title_key(title), []).append((names, monitor.normalize(album)))
    c._loaded = True
    return c


LIB = checker([
    ("Ninajirachi; BRUX", "How Could U", "How Could U"),
    ("Ninajirachi; Porter Robinson", "WannaCry", "WannaCry"),
    ("Jane Remover", "Frailty", "buzzcut, daisy"),
    ("KAYTRANADA; PinkPantheress", "TIMELESS", "Snap My Finger"),
    ("Oklou", "harvest sky (remixes)", "harvest sky (Milkfish remix)"),
])


def test_collaborator_credit_finds_the_track():
    assert LIB.find_track("Brux", "How Could U") == "how could u"
    assert LIB.find_track("Porter Robinson", "WANNACRY") == "wannacry"


def test_artist_alias_found_through_the_album_name():
    assert LIB.find_track("dltzk", "buzzcut, daisy", "frailty") == "frailty"
    assert LIB.find_track("dltzk", "buzzcut, daisy", "some other album") is None


def test_feat_credit_in_the_title_is_ignored():
    assert LIB.find_track("KAYTRANADA", "Snap My Finger (feat. PinkPantheress)") == "timeless"


def test_a_remix_is_not_the_original():
    assert LIB.find_track("Oklou", "harvest sky") is None
    assert LIB.find_track("Oklou", "harvest sky (Milkfish Remix)") == "harvest sky (remixes)"


def test_same_title_by_someone_else_is_not_a_match():
    assert LIB.find_track("Somebody Else", "How Could U") is None

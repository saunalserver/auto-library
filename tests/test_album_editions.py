"""Album-edition matching.

Real case, 2026-09-02: a scrobble for "Oklou - choke enough (Deluxe)" (an
edition Tidal does not carry) downloaded "choke enough (remixes)" instead of
the base album, because SequenceMatcher scores "remixes" closer to "deluxe"
than the empty string is. Asking for one edition must never pull in a
different one.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from smart_download import album_score, base_title, edition  # noqa: E402


def test_edition_and_base_title_parsing():
    assert base_title("choke enough (Deluxe)") == "choke enough"
    assert edition("choke enough (Deluxe)") == "deluxe"
    assert edition("choke enough") == ""
    assert base_title("RAVE:N, The Remixes") == "rave n the remixes"


def test_missing_edition_prefers_the_base_album_over_another_edition():
    want = "choke enough (Deluxe)"
    assert album_score("choke enough", want) > album_score("choke enough (remixes)", want)


def test_exact_edition_still_wins_when_it_exists():
    want = "Wallsocket (Director's Cut)"
    assert album_score("Wallsocket (Director's Cut)", want) > album_score("Wallsocket", want)
    assert album_score("Wallsocket (Director's Cut)", want) > album_score("Wallsocket (Deluxe)", want)


def test_plain_request_prefers_plain_album_over_a_remix_edition():
    want = "Souvlaki"
    assert album_score("Souvlaki", want) > album_score("Souvlaki (Remixes)", want)


def test_unrelated_albums_still_score_low():
    assert album_score("Completely Different Record", "choke enough (Deluxe)") < 0.5


def test_punctuation_only_differences_still_match():
    # Tidal sanitises characters out of titles; those are the same album.
    assert album_score("RAVEN, The Remixes", "RAVE:N, The Remixes") > 0.9

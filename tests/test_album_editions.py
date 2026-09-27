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

from smart_download import album_score, base_title, edition, strip_artist_prefix  # noqa: E402


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


# Real cases, 2026-09-06: the Sunday discovery run downloaded Mahler's
# Symphony No. 2 for "Mahler: Symphony No. 5", and a 154-track
# "Mendelssohn - Great Recordings" box for "Mendelssohn: Piano Pieces".

def test_a_different_number_is_a_different_album():
    want = "Mahler: Symphony No. 5"
    assert album_score("Symphony No. 2 (Live)", want, "Gustav Mahler") == 0.0
    assert album_score("Symphony No.5 in C sharp minor", want, "Gustav Mahler") > 0.5
    assert album_score("Drop 6", "DROP 7") == 0.0


def test_numbers_in_an_edition_do_not_count():
    # "(2018 Remaster)" is an edition, not part of the album's name.
    assert album_score("Hounds of Love", "Hounds of Love (2018 Remaster)") > 0.5


def test_composer_prefix_does_not_make_unrelated_albums_similar():
    artist = "Felix Mendelssohn"
    assert strip_artist_prefix("Mendelssohn: Piano Pieces", artist) == "Piano Pieces"
    assert strip_artist_prefix("Mendelssohn - Great Recordings", artist) == "Great Recordings"
    assert album_score("Mendelssohn - Great Recordings", "Mendelssohn: Piano Pieces", artist) < 0.5
    # A prefix that is not the artist's name is part of the title.
    assert strip_artist_prefix("Kiss Land: Remixes", "The Weeknd") == "Kiss Land: Remixes"

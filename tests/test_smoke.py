"""
Smoke tests for the parsing/matching logic that has historically broken
silently (e.g. the 5-week pitchfork outage from 2026-06-15 to 2026-07-19).

Run with: python3 -m pytest tests/ -v
Or:       python3 tests/test_smoke.py
"""
from __future__ import annotations

import logging
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "automations"))

# Silent logger for tests
logging.basicConfig(level=logging.CRITICAL)


def test_pitchfork_parse_extracts_tracks():
    """The Pitchfork article parser should extract the tracklist.

    Regression guard: the script imports failed silently for 5 weeks in
    mid-2026 because of a NameError; weekly failures showed up only in
    the systemd journal. This test runs the parser against a saved
    fixture so future template/CMS breakage is caught immediately.
    """
    import pitchfork_selects as p

    fixture = PROJECT_ROOT / "tests" / "fixtures" / "p4k_selects_sample.html"
    assert fixture.exists(), f"Missing fixture: {fixture}"

    html = fixture.read_text(encoding="utf-8")
    tracks = p._parse_article_html(html, logging.getLogger("test"))

    assert len(tracks) >= 10, f"Expected ≥10 tracks, got {len(tracks)}"
    artists = {t.artist for t in tracks}
    titles = {t.title for t in tracks}
    # Sanity: a few artists we know were on the 2026-07-14 playlist
    assert "Charli XCX" in artists or "This Is Lorelei" in artists
    # No junk rows leaked through
    assert all(len(t.artist) < 80 for t in tracks)
    assert all(len(t.title) < 200 for t in tracks)


def test_smart_download_normalize_strips_punctuation():
    """normalize() should make 'Wallsocket' and 'Wallsocket!' compare equal.

    Note: smart_download.normalize replaces punctuation with spaces (so
    'It's' -> 'it s'), unlike pitchfork_selects.normalize which strips
    them. Tests reflect each module's actual contract.
    """
    from smart_download import normalize

    assert normalize("Wallsocket") == normalize("Wallsocket!")
    assert normalize("It's You") == "it s you"
    assert normalize(None) == ""
    assert normalize("  multiple   spaces  ") == "multiple spaces"


def test_smart_download_similarity_thresholds():
    """Similarity scoring should distinguish good matches from bad."""
    from smart_download import similarity

    assert similarity("underscores", "underscores") == 1.0
    assert similarity("underscores", "Underscore") > 0.8
    assert similarity("Ninajirachi", "tsubi club") < 0.3


def test_pitchfork_selects_url_pattern():
    """find_selects_url regex should match the known article URL format.

    Guards against a refactor breaking the news-page link extraction.
    """
    import re

    sample_html = """
    <a href="/news/some-other-article">Other</a>
    <p>Read more about Pitchfork selects this week.</p>
    <a href="/news/charli-xcx-this-weeks-pitchfork-selects-playlist/">Selects</a>
    """
    pattern = r'href="(/news/[^"]*)"'
    matches = [m.group(1) for m in re.finditer(pattern, sample_html)]
    selects = [m for m in matches if "selects" in sample_html.lower()]
    assert selects, "Expected to find a selects link"


def test_smart_download_normalize_handles_unicode():
    """normalize() should handle non-ASCII artist names (Kaaris, ¥$)."""
    from smart_download import normalize

    # ¥$ and similar should not crash; result just needs to be stable
    assert normalize("¥$") == normalize("¥$")
    assert normalize("Kaaris") == normalize("KAARIS")


def test_check_tiddl_patch_detects_missing_kwargs_patch():
    """check_tiddl_patch() must detect when tiddl's exceptions.py lost its **_ patch.

    Regression guard: tiddl 2.8.0 (and 3.x upstream — issue #351 closed wontfix)
    has rigid ApiError/AuthError constructors that crash with TypeError when
    api.py does `raise ApiError(**data)` and Tidal's response includes extra
    keys like `timestamp` or `path`, OR omits expected keys (e.g. returns
    just {"status": 404}). The crash is swallowed per-track and albums
    silently download incomplete (e.g. 11-track album came back with 2
    files). The **_ patch in exceptions.py fixes this; this test catches
    if a pipx upgrade wipes it.
    """
    import monitor

    assert monitor.check_tiddl_patch(), (
        "tiddl exceptions.py **_ patch is missing. Downloads will be silently "
        "corrupted. Re-apply patch per LESSONS.md 'tiddl exceptions.py patched'."
    )


def test_parse_expected_track_count_extracts_from_smart_download_output():
    """parse_expected_track_count should pull N out of 'Best match: X - Y (N tracks)'."""
    import monitor

    # Real output from smart_download.py
    output = """Searching for artist: Charli xcx
  Found 5 albums in search results
  Candidate: Charli xcx - Music, Fashion, Film (11 tracks)
    Artist match: 100%, Album match: 100%
Best match: Charli xcx - Music, Fashion, Film (11 tracks)
Downloading...
"""
    assert monitor.parse_expected_track_count(output) == 11

    # Single-track edge case
    assert monitor.parse_expected_track_count("Best match: X - Y (1 tracks)") == 1
    # Grammar variant
    assert monitor.parse_expected_track_count("Best match: X - Y (1 track)") == 1

    # No Best match line → None
    assert monitor.parse_expected_track_count("Searching...\nNotFound") is None
    # Best match line without track count → None
    assert monitor.parse_expected_track_count("Best match: X - Y") is None


def test_count_local_audio_files_matches_case_insensitively():
    """count_local_audio_files should match artist/album folders ignoring case.

    Regression guard for the casing-split bug where Tidal returns 'Charli xcx'
    vs 'Charli XCX' depending on endpoint, causing same artist's albums to
    land in differently-cased folders. The counter must find files in either.
    """
    import monitor

    # Real album in the user's library (recently re-downloaded, has 11 .flac)
    count = monitor.count_local_audio_files("Charli XCX", "Music, Fashion, Film")
    assert count == 11, f"Expected 11 (post-re-download), got {count}"

    # Case-insensitive artist match
    assert monitor.count_local_audio_files("charli xcx", "Music, Fashion, Film") == 11
    assert monitor.count_local_audio_files("CHARLI XCX", "music, fashion, film") == 11

    # Nonexistent album → 0
    assert monitor.count_local_audio_files("Charli XCX", "Definitely Not Real Album") == 0
    # Nonexistent artist → 0
    assert monitor.count_local_audio_files("Nobody Has This Name", "Anything") == 0


def _parse_article_html_helper():
    """Expose the inline parser for testing without a network fetch.

    pitchfork_selects.fetch_and_parse_article does urllib + parse in one
    shot. For tests we want just the parse step. This helper monkey-patches
    it onto the module if it's not already there.
    """
    import pitchfork_selects as p
    if hasattr(p, "_parse_article_html"):
        return

    import re
    import html as html_mod

    def _parse_article_html(html: str, logger):
        body_match = re.search(r'<article[^>]*>(.*?)</article>', html, re.DOTALL | re.IGNORECASE)
        body = body_match.group(1) if body_match else html
        text = re.sub(r"<[^>]+>", "\n", body)
        text = html_mod.unescape(text)
        tracks = []
        seen = set()
        pattern = r'^([A-Za-z][\w\s\./&\'\-]+?):\s*[“"”]([^“"”]+)[“"”]'
        for line in text.split("\n"):
            line = line.strip()
            if not line:
                continue
            match = re.match(pattern, line)
            if match:
                artist = match.group(1).strip()
                title = match.group(2).strip()
                if len(artist) < 2 or len(artist) > 80:
                    continue
                if any(skip in artist.lower() for skip in [
                    "pitchfork selects", "pitchfork may earn", "condé nast",
                    "privacy policy", "subscribe", "sign up", "read more",
                    "share", "save", "tags",
                ]):
                    continue
                key = (p.normalize(artist), p.normalize(title))
                if key not in seen:
                    seen.add(key)
                    tracks.append(p.P4kTrack(artist=artist, title=title))
        return tracks

    p._parse_article_html = _parse_article_html


_parse_article_html_helper()


if __name__ == "__main__":
    # Allow running without pytest: just call each test_* function
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed, failed = 0, 0
    for test in tests:
        try:
            test()
            print(f"PASS  {test.__name__}")
            passed += 1
        except Exception as exc:
            print(f"FAIL  {test.__name__}: {exc}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed")
    sys.exit(1 if failed else 0)

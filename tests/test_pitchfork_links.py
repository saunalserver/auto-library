"""Pitchfork moved Selects articles from /news/ to /story/ in Aug 2026 and the
old regex failed every Monday for a month. Guard both URL shapes."""
from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "automations"))

import pitchfork_selects as p  # noqa: E402


def test_extract_story_links():
    html = ('<a href="/story/ice-spice-this-weeks-pitchfork-selects-playlist/">x</a>'
            '<a href="/story/unrelated-review/">y</a>')
    assert p._extract_selects_links(html) == ["/story/ice-spice-this-weeks-pitchfork-selects-playlist/"]


def test_extract_legacy_news_links_with_context():
    html = ('<div>This Week’s Pitchfork Selects Playlist</div>'
            '<a href="/news/charli-xcx-playlist/">Selects</a>')
    assert p._extract_selects_links(html) == ["/news/charli-xcx-playlist/"]


def test_extract_absolute_links_and_dedupe():
    html = ('<a href="https://pitchfork.com/story/a-selects-playlist/">1</a>'
            '<a href="https://pitchfork.com/story/a-selects-playlist/">2</a>')
    assert p._extract_selects_links(html) == ["https://pitchfork.com/story/a-selects-playlist/"]


def test_parse_article_html_real_fixture():
    fixture = PROJECT_ROOT / "tests" / "fixtures" / "p4k_selects_sample.html"
    tracks = p._parse_article_html(fixture.read_text(encoding="utf-8"), __import__("logging").getLogger("t"))
    assert len(tracks) >= 10

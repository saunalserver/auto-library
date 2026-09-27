"""Reading your ntfy reply to the weekly discovery list."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from discovery_picks import parse_reply  # noqa: E402


def test_numbers_in_any_separator_style():
    assert parse_reply("1,3,5,9", 10) == [1, 3, 5, 9]
    assert parse_reply("1 3 5", 10) == [1, 3, 5]
    assert parse_reply("(1,3,5,9)".strip("()"), 10) == [1, 3, 5, 9]
    assert parse_reply("#2 and #4", 10) == [2, 4]
    assert parse_reply(" 2, 2, 7 ", 10) == [2, 7]


def test_ranges():
    assert parse_reply("2-4", 10) == [2, 3, 4]
    assert parse_reply("1, 6–8", 10) == [1, 6, 7, 8]


def test_all_and_none():
    assert parse_reply("all", 4) == [1, 2, 3, 4]
    assert parse_reply("ALL", 2) == [1, 2]
    assert parse_reply("none", 4) == []
    assert parse_reply("skip", 4) == []


def test_numbers_outside_the_list_are_dropped():
    assert parse_reply("3, 12", 10) == [3]
    assert parse_reply("12", 10) is None


def test_ordinary_text_is_not_an_order():
    # Anything that is not clearly a pick must not trigger downloads.
    assert parse_reply("the 2 albums from last week were great", 10) is None
    assert parse_reply("hello", 10) is None
    assert parse_reply("", 10) is None

"""Tests for split_source_prefix, extract_schedule_minutes, and r/ subreddit parsing."""

from __future__ import annotations

from sns_monitor.filters import (
    extract_schedule_minutes,
    parse_account_watch_text,
    split_source_prefix,
)


# ── split_source_prefix ──────────────────────────────────────────────────────


def test_split_source_prefix_reddit() -> None:
    assert split_source_prefix("reddit:r/PokemonTCG") == ("reddit", "r/PokemonTCG")
    assert split_source_prefix("REDDIT:r/yugioh") == ("reddit", "r/yugioh")
    assert split_source_prefix("reddit: r/pokemon ") == ("reddit", "r/pokemon")


def test_split_source_prefix_x() -> None:
    assert split_source_prefix("x:@elonmusk") == ("x", "@elonmusk")
    assert split_source_prefix("X:keyword:foo") == ("x", "keyword:foo")


def test_split_source_prefix_backcompat_default_is_x() -> None:
    # Bare @handle / keyword: / trend: still routes to X for backcompat.
    assert split_source_prefix("@elonmusk") == ("x", "@elonmusk")
    assert split_source_prefix("keyword:foo") == ("x", "keyword:foo")
    assert split_source_prefix("trend:trending") == ("x", "trend:trending")
    assert split_source_prefix("r/PokemonTCG") == ("x", "r/PokemonTCG")  # no prefix → x


# ── extract_schedule_minutes ─────────────────────────────────────────────────


def test_extract_schedule_minutes_default_token() -> None:
    minutes, remainder = extract_schedule_minutes("@elonmusk schedule:30 filter[buy]")
    assert minutes == 30
    assert "schedule" not in remainder
    assert "@elonmusk" in remainder
    assert "filter[buy]" in remainder


def test_extract_schedule_minutes_equals_form() -> None:
    minutes, remainder = extract_schedule_minutes("schedule=60 @x")
    assert minutes == 60
    assert "schedule" not in remainder


def test_extract_schedule_minutes_missing_returns_none() -> None:
    minutes, remainder = extract_schedule_minutes("@elonmusk filter[buy]")
    assert minutes is None
    assert remainder == "@elonmusk filter[buy]"


def test_extract_schedule_minutes_clamps_out_of_range() -> None:
    # Below 5min → reject (too aggressive against Reddit rate limit)
    minutes, _ = extract_schedule_minutes("@x schedule:1")
    assert minutes is None
    # Above 1440min (24h) → reject (effectively disabled)
    minutes, _ = extract_schedule_minutes("@x schedule:9999")
    assert minutes is None


# ── r/subreddit parsing ──────────────────────────────────────────────────────


def test_parse_account_watch_text_accepts_subreddit_form() -> None:
    result = parse_account_watch_text("r/PokemonTCG")
    assert result is not None
    handle, kw, domains = result
    assert handle == "PokemonTCG"
    assert kw == ()
    assert domains is None


def test_parse_account_watch_text_accepts_subreddit_with_domain() -> None:
    result = parse_account_watch_text("r/PokemonTCG domain[pokemon]")
    assert result is not None
    handle, _, domains = result
    assert handle == "PokemonTCG"
    assert domains == ("pokemon",)


def test_parse_account_watch_text_rejects_invalid_subreddit() -> None:
    # Single char name is below Reddit's 2-char minimum
    assert parse_account_watch_text("r/a") is None
    # 22 chars exceeds Reddit's 21-char limit
    assert parse_account_watch_text(f"r/{'a' * 22}") is None


def test_parse_account_watch_text_still_handles_at_handle() -> None:
    # Backcompat: @handle path must still work after r/ branch was added.
    result = parse_account_watch_text("@elonmusk")
    assert result is not None
    assert result[0] == "elonmusk"

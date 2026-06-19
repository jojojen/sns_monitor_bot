"""Tests for split_source_prefix, extract_schedule_minutes, and URL rewriting."""

from __future__ import annotations

from sns_monitor.filters import (
    extract_schedule_minutes,
    parse_account_watch_text,
    rewrite_social_url,
    split_source_prefix,
)


# ── split_source_prefix ──────────────────────────────────────────────────────


def test_split_source_prefix_x() -> None:
    assert split_source_prefix("x:@elonmusk") == ("x", "@elonmusk")
    assert split_source_prefix("X:keyword:foo") == ("x", "keyword:foo")


def test_split_source_prefix_backcompat_default_is_x() -> None:
    # Bare @handle / keyword: / trend: still routes to X for backcompat.
    assert split_source_prefix("@elonmusk") == ("x", "@elonmusk")
    assert split_source_prefix("keyword:foo") == ("x", "keyword:foo")
    assert split_source_prefix("trend:trending") == ("x", "trend:trending")


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
    # Below 5min → reject (too aggressive against host rate limits)
    minutes, _ = extract_schedule_minutes("@x schedule:1")
    assert minutes is None
    # Above 1440min (24h) → reject (effectively disabled)
    minutes, _ = extract_schedule_minutes("@x schedule:9999")
    assert minutes is None


# ── @handle parsing ──────────────────────────────────────────────────────────


def test_parse_account_watch_text_handles_at_handle() -> None:
    result = parse_account_watch_text("@elonmusk")
    assert result is not None
    assert result[0] == "elonmusk"


# ── rewrite_social_url ───────────────────────────────────────────────────────


def test_rewrite_x_url_with_share_query() -> None:
    # The reported case: pasted profile URL with the ?s=NN share suffix.
    assert rewrite_social_url("https://x.com/pcgl_shibuya?s=21") == "@pcgl_shibuya"


def test_rewrite_x_url_full_pipeline_resolves_handle() -> None:
    out = rewrite_social_url("https://x.com/pcgl_shibuya?s=21")
    source, body = split_source_prefix(out)
    assert source == "x"
    result = parse_account_watch_text(body)
    assert result is not None and result[0] == "pcgl_shibuya"


def test_rewrite_twitter_and_nitter_and_status_url() -> None:
    assert rewrite_social_url("https://twitter.com/elonmusk") == "@elonmusk"
    assert rewrite_social_url("https://nitter.net/elonmusk") == "@elonmusk"
    # A status/tweet URL still resolves to the author handle (first segment).
    assert rewrite_social_url("https://x.com/jack/status/20") == "@jack"


def test_rewrite_x_url_preserves_trailing_tokens() -> None:
    out = rewrite_social_url("https://x.com/foo?s=21 filter[抽選] domain[pokemon] schedule:30")
    assert out == "@foo filter[抽選] domain[pokemon] schedule:30"


def test_rewrite_schemeless_url() -> None:
    assert rewrite_social_url("x.com/pcgl_shibuya") == "@pcgl_shibuya"


def test_rewrite_reddit_url_left_untouched() -> None:
    # Reddit support is removed; reddit URLs are no longer recognized and pass
    # through unchanged rather than being rewritten to a reddit: prefix.
    assert (
        rewrite_social_url("https://www.reddit.com/r/PokemonTCG/")
        == "https://www.reddit.com/r/PokemonTCG/"
    )


def test_rewrite_ignores_reserved_x_paths() -> None:
    # Feature paths are not accounts → left unchanged so they fall through.
    assert rewrite_social_url("https://x.com/search?q=foo") == "https://x.com/search?q=foo"
    assert rewrite_social_url("https://x.com/i/lists/123") == "https://x.com/i/lists/123"


def test_rewrite_passes_through_non_url_forms() -> None:
    # Existing command forms must be untouched.
    assert rewrite_social_url("@elonmusk") == "@elonmusk"
    assert rewrite_social_url("x:keyword:foo") == "x:keyword:foo"
    assert rewrite_social_url("keyword:機動戰士 domain[gundam]") == "keyword:機動戰士 domain[gundam]"

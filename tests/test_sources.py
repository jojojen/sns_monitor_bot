"""Contract tests for SnsSource plugins."""

from __future__ import annotations

import pytest

from sns_monitor.models import Tweet
from sns_monitor.sources import RedditSource, SnsSource, XSource, build_default_sources


# ── Protocol conformance ─────────────────────────────────────────────────────


def test_x_source_satisfies_protocol() -> None:
    fake_client = _FakeXClient()
    src = XSource(fake_client)
    assert isinstance(src, SnsSource)
    assert src.name == "x"
    assert src.default_account_schedule_minutes == 15
    assert src.default_trend_schedule_minutes == 60


def test_reddit_source_satisfies_protocol() -> None:
    src = RedditSource()
    assert isinstance(src, SnsSource)
    assert src.name == "reddit"
    assert src.default_account_schedule_minutes == 30
    # Reddit explicitly does NOT support trend watches — None signals "off"
    # to the bot UX so trend rules can't be created against this source.
    assert src.default_trend_schedule_minutes is None


def test_build_default_sources_includes_reddit_unconditionally() -> None:
    sources = build_default_sources(x_client=None)
    assert "reddit" in sources
    assert "x" not in sources  # x_client=None → no XSource registered

    sources2 = build_default_sources(x_client=_FakeXClient())
    assert "reddit" in sources2
    assert "x" in sources2


# ── Reddit source ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reddit_source_rejects_invalid_subreddit_names() -> None:
    src = RedditSource(client=_StubRedditClient())
    assert await src.fetch_account("r/", user_id=None) == []
    assert await src.fetch_account("r/!!", user_id=None) == []
    # 22 chars exceeds Reddit's 21-char subreddit name limit
    assert await src.fetch_account("r/" + "a" * 22, user_id=None) == []


@pytest.mark.asyncio
async def test_reddit_source_strips_r_prefix_and_uses_stub() -> None:
    stub = _StubRedditClient()
    src = RedditSource(client=stub)
    posts = await src.fetch_account("r/PokemonTCG", user_id=None)
    assert stub.last_subreddit == "PokemonTCG"
    assert len(posts) == 1
    assert posts[0].author_handle == "r/PokemonTCG"


@pytest.mark.asyncio
async def test_reddit_source_resolve_user_id_returns_subreddit_name() -> None:
    src = RedditSource()
    assert await src.resolve_user_id("r/PokemonTCG") == "PokemonTCG"
    assert await src.resolve_user_id("PokemonTCG") == "PokemonTCG"
    assert await src.resolve_user_id("") is None
    assert await src.resolve_user_id("r/has spaces") is None


@pytest.mark.asyncio
async def test_reddit_source_fetch_trend_raises() -> None:
    src = RedditSource()
    with pytest.raises(NotImplementedError):
        await src.fetch_trend("trending")


# ── X source ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_x_source_fetch_account_requires_user_id() -> None:
    fake_client = _FakeXClient()
    src = XSource(fake_client)
    # XClientWeb requires a numeric user_id; without it, return empty rather
    # than fan out a bogus request.
    assert await src.fetch_account("elonmusk", user_id=None) == []
    assert fake_client.timeline_calls == 0


@pytest.mark.asyncio
async def test_x_source_fetch_account_passes_user_id_through() -> None:
    fake_client = _FakeXClient()
    src = XSource(fake_client)
    await src.fetch_account("elonmusk", user_id="12345")
    assert fake_client.last_user_id == "12345"


# ── Test helpers ──────────────────────────────────────────────────────────────


class _FakeXClient:
    def __init__(self) -> None:
        self.timeline_calls = 0
        self.last_user_id: str | None = None

    async def ensure_logged_in(self) -> None:
        return None

    async def get_timeline(self, user_id: str, *, count: int = 20):
        self.timeline_calls += 1
        self.last_user_id = user_id
        return []

    async def search(self, query: str, *, count: int = 15):
        return []

    async def get_trends(self, category: str, *, count: int = 20):
        return []

    async def resolve_user_id(self, screen_name: str) -> str:
        return f"id_{screen_name}"


class _StubRedditClient:
    def __init__(self) -> None:
        self.last_subreddit: str | None = None

    async def fetch_subreddit_hot(self, name: str, *, count: int = 25):
        from datetime import datetime, timezone

        self.last_subreddit = name
        return [
            Tweet(
                tweet_id="abc",
                author_handle=f"r/{name}",
                author_id="u/someone",
                text="hello",
                created_at=datetime.now(timezone.utc),
            )
        ]

    async def search(self, query: str, *, count: int = 25, window: str | None = None):
        return []

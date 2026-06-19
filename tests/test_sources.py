"""Contract tests for SnsSource plugins."""

from __future__ import annotations

import pytest

from sns_monitor.models import Tweet
from sns_monitor.sources import SnsSource, XSource, build_default_sources


# ── Protocol conformance ─────────────────────────────────────────────────────


def test_x_source_satisfies_protocol() -> None:
    fake_client = _FakeXClient()
    src = XSource(fake_client)
    assert isinstance(src, SnsSource)
    assert src.name == "x"
    assert src.default_account_schedule_minutes == 15
    assert src.default_trend_schedule_minutes == 60


def test_build_default_sources_registers_x_only_when_client_given() -> None:
    # Reddit has been removed entirely; no source is registered without a client.
    sources = build_default_sources(x_client=None)
    assert "reddit" not in sources
    assert "x" not in sources

    sources2 = build_default_sources(x_client=_FakeXClient())
    assert "reddit" not in sources2
    assert "x" in sources2


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

"""X (Twitter) source plugin — thin adapter over the existing X client.

Wraps either `XClient` (twikit) or `XClientWeb` (Nitter RSS); both expose
the same async surface (`ensure_logged_in`, `get_timeline`, `search`,
`get_trends`, `resolve_user_id`), so this adapter is purely a protocol
shim — no behavior change.
"""

from __future__ import annotations

from ..models import Tweet


class XSource:
    name = "x"
    default_account_schedule_minutes = 15
    default_keyword_schedule_minutes = 30
    default_trend_schedule_minutes: int | None = 60

    def __init__(self, x_client) -> None:
        self._x = x_client

    async def ensure_logged_in(self) -> None:
        await self._x.ensure_logged_in()

    async def fetch_account(self, target: str, *, user_id: str | None) -> list[Tweet]:
        if not user_id:
            return []
        return await self._x.get_timeline(user_id)

    async def search_keyword(self, query: str) -> list[Tweet]:
        return await self._x.search(query)

    async def fetch_trend(self, category: str) -> list[str]:
        return await self._x.get_trends(category)

    async def resolve_user_id(self, target: str) -> str | None:
        screen_name = target.lstrip("@").strip()
        if not screen_name:
            return None
        try:
            return await self._x.resolve_user_id(screen_name)
        except Exception:
            return None

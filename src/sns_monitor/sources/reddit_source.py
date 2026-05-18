"""Reddit source plugin — periodic subreddit + keyword polling.

Reuses `RedditBuzzClient` (already battle-tested by `/snsbuzz`) for the
HTTP/parse layer; adds a SnsSource adapter that:
- treats `target` as the subreddit name (r/PokemonTCG → "PokemonTCG"),
- maps subreddit name → user_id 1:1 (subreddit name is the stable identifier),
- declines `fetch_trend` (Reddit has no native trending API).

Default cadence is 30/60 min (account / keyword); Reddit's anon API limits
are generous (~60 req/min/IP) so we stay comfortably under load.
"""

from __future__ import annotations

import re

from ..models import Tweet
from ..reddit_buzz import RedditBuzzClient

_SUBREDDIT_NAME_RE = re.compile(r"^[A-Za-z0-9_]{2,21}$")


class RedditSource:
    name = "reddit"
    default_account_schedule_minutes = 30
    default_keyword_schedule_minutes = 60
    default_trend_schedule_minutes: int | None = None

    def __init__(self, *, client: RedditBuzzClient | None = None) -> None:
        self._client = client or RedditBuzzClient()

    async def ensure_logged_in(self) -> None:
        return None

    async def fetch_account(self, target: str, *, user_id: str | None) -> list[Tweet]:
        name = (user_id or target).strip().lstrip("/").removeprefix("r/").strip("/")
        if not name or not _SUBREDDIT_NAME_RE.match(name):
            return []
        return await self._client.fetch_subreddit_hot(name, count=25)

    async def search_keyword(self, query: str) -> list[Tweet]:
        if not query.strip():
            return []
        return await self._client.search(query, count=25)

    async def fetch_trend(self, category: str) -> list[str]:
        raise NotImplementedError("Reddit source does not support trend watches")

    async def resolve_user_id(self, target: str) -> str | None:
        name = target.strip().lstrip("/").removeprefix("r/").strip("/")
        if not name or not _SUBREDDIT_NAME_RE.match(name):
            return None
        return name

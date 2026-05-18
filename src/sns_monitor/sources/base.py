"""SnsSource Protocol — uniform interface for SNS data backends."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..models import Tweet


@runtime_checkable
class SnsSource(Protocol):
    """Interface every SNS backend must implement.

    Conventions:
    - `name` is the source key used in WatchRule.source and SOURCES registry.
    - Default schedule attributes are per-rule fallbacks when the user didn't
      explicitly supply `schedule:NN`.
    - Methods that a source doesn't support (e.g. Reddit doesn't have trends)
      should raise `NotImplementedError` rather than return empty silently —
      surfaced loudly so misconfigured rules don't silently no-op.
    """

    name: str
    default_account_schedule_minutes: int
    default_keyword_schedule_minutes: int
    default_trend_schedule_minutes: int | None

    async def ensure_logged_in(self) -> None:
        """Optional warm-up. Sources that need no auth may no-op."""
        ...

    async def fetch_account(self, target: str, *, user_id: str | None) -> list[Tweet]:
        """Return recent posts from a single account/subreddit/channel."""
        ...

    async def search_keyword(self, query: str) -> list[Tweet]:
        """Return recent posts matching a keyword."""
        ...

    async def fetch_trend(self, category: str) -> list[str]:
        """Return current trending topic names. May raise NotImplementedError."""
        ...

    async def resolve_user_id(self, target: str) -> str | None:
        """Map a public identifier (@handle, r/sub) to a stable internal id."""
        ...

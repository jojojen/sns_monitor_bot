from __future__ import annotations

import asyncio
import logging
import random
import time
from datetime import datetime
from pathlib import Path

import twikit

from .models import Tweet

logger = logging.getLogger(__name__)


def _convert_tweet(t: twikit.Tweet) -> Tweet:
    """Convert twikit.Tweet to our Tweet model."""
    handle = t.user.screen_name if t.user else "unknown"
    user_id = t.user.id if t.user else ""

    created_at = t.created_at
    if isinstance(created_at, str):
        created_at = datetime.strptime(created_at, "%a %b %d %H:%M:%S %z %Y")

    return Tweet(
        tweet_id=t.id,
        author_handle=handle,
        author_id=user_id,
        text=t.text,
        created_at=created_at,
        lang=getattr(t, "lang", None),
        retweet_count=t.retweet_count,
        like_count=t.favorite_count,
        url=f"https://x.com/{handle}/status/{t.id}",
    )


async def _human_delay() -> None:
    """Sleep for a random duration to mimic human browsing."""
    await asyncio.sleep(random.uniform(1.5, 4.0))


class XClient:
    """Async twikit client with circuit breaker and human-like behavior."""

    _COOLDOWN_SECONDS = 600.0
    _RATE_LIMIT_BACKOFF = 900.0

    def __init__(
        self,
        *,
        username: str,
        email: str,
        password: str,
        cookies_file: str | Path = "cookies.json",
        language: str = "ja",
    ) -> None:
        self._username = username
        self._email = email
        self._password = password
        self._cookies_file = str(cookies_file)
        self._language = language
        self._client: twikit.Client | None = None
        self._disabled_until: float = 0.0
        self._lock = asyncio.Lock()

    async def ensure_logged_in(self) -> None:
        """Login if needed, loading cookies from file if they exist."""
        async with self._lock:
            if self._client is not None:
                return

            logger.info("Logging into X with email=%s username=%s", self._email, self._username)
            client = twikit.Client(language=self._language)
            try:
                await client.login(
                    auth_info_1=self._username,
                    auth_info_2=self._email,
                    password=self._password,
                    cookies_file=self._cookies_file,
                )
                self._client = client
                logger.info("Successfully logged into X")
            except Exception as e:
                logger.exception("Failed to login to X: %s", e)
                raise

    async def get_timeline(self, user_id: str, *, count: int = 20) -> list[Tweet]:
        """Fetch user timeline."""
        if self._is_disabled():
            return []

        try:
            await self.ensure_logged_in()
            await _human_delay()
            result = await self._client.get_user_tweets(user_id, "Tweets", count=count)
            return [_convert_tweet(t) for t in list(result)]
        except twikit.TooManyRequests:
            self._trip_circuit(self._RATE_LIMIT_BACKOFF)
            return []
        except (twikit.Unauthorized, twikit.AccountSuspended, twikit.AccountLocked):
            self._client = None
            Path(self._cookies_file).unlink(missing_ok=True)
            self._trip_circuit(self._COOLDOWN_SECONDS)
            return []
        except Exception:
            logger.exception("XClient.get_timeline failed user_id=%s", user_id)
            return []

    async def resolve_user_id(self, screen_name: str) -> str:
        """Resolve screen name to user ID."""
        if self._is_disabled():
            return ""

        try:
            await self.ensure_logged_in()
            await _human_delay()
            user = await self._client.get_user_by_screen_name(screen_name.lstrip("@"))
            return user.id if user else ""
        except twikit.TooManyRequests:
            self._trip_circuit(self._RATE_LIMIT_BACKOFF)
            return ""
        except (twikit.Unauthorized, twikit.AccountSuspended, twikit.AccountLocked):
            self._client = None
            Path(self._cookies_file).unlink(missing_ok=True)
            self._trip_circuit(self._COOLDOWN_SECONDS)
            return ""
        except Exception:
            logger.exception("XClient.resolve_user_id failed screen_name=%s", screen_name)
            return ""

    async def search(self, query: str, *, count: int = 20) -> list[Tweet]:
        """Search for tweets matching query."""
        if self._is_disabled():
            return []

        try:
            await self.ensure_logged_in()
            await _human_delay()
            result = await self._client.search_tweet(query, "Latest", count=count)
            return [_convert_tweet(t) for t in list(result)]
        except twikit.TooManyRequests:
            self._trip_circuit(self._RATE_LIMIT_BACKOFF)
            return []
        except (twikit.Unauthorized, twikit.AccountSuspended, twikit.AccountLocked):
            self._client = None
            Path(self._cookies_file).unlink(missing_ok=True)
            self._trip_circuit(self._COOLDOWN_SECONDS)
            return []
        except Exception:
            logger.exception("XClient.search failed query=%s", query)
            return []

    async def get_trends(self, category: str = "trending", *, count: int = 20) -> list[str]:
        """Fetch trending topic names."""
        if self._is_disabled():
            return []

        try:
            await self.ensure_logged_in()
            await _human_delay()
            trends = await self._client.get_trends(category)
            return [t.name for t in list(trends)[:count]]
        except twikit.TooManyRequests:
            self._trip_circuit(self._RATE_LIMIT_BACKOFF)
            return []
        except (twikit.Unauthorized, twikit.AccountSuspended, twikit.AccountLocked):
            self._client = None
            Path(self._cookies_file).unlink(missing_ok=True)
            self._trip_circuit(self._COOLDOWN_SECONDS)
            return []
        except Exception:
            logger.exception("XClient.get_trends failed category=%s", category)
            return []

    def _is_disabled(self) -> bool:
        return time.monotonic() < self._disabled_until

    def _trip_circuit(self, cooldown: float) -> None:
        self._disabled_until = time.monotonic() + cooldown
        logger.warning("XClient circuit tripped cooldown_seconds=%.0f", cooldown)

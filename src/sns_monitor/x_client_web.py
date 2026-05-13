from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Optional

from .models import Tweet

logger = logging.getLogger(__name__)


# Nitter mirrors to try in order; first working one wins.
NITTER_HOSTS = (
    "nitter.net",
    "nitter.privacydev.net",
    "nitter.poast.org",
    "xcancel.com",
)

_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


async def _human_delay() -> None:
    """Sleep for a random duration to mimic human browsing."""
    await asyncio.sleep(random.uniform(1.5, 4.0))


_BROWSER_HEADERS = {
    "User-Agent": _USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,ja;q=0.8,zh-TW;q=0.7",
    "Accept-Encoding": "identity",  # urllib doesn't auto-decompress gzip
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
}


def _fetch(url: str, timeout: float = 15.0, *, headers: dict | None = None) -> str:
    """Blocking HTTP GET helper (runs in executor)."""
    h = dict(_BROWSER_HEADERS)
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")




class XClientWeb:
    """Hybrid social client.

    - `get_timeline` fetches X (Twitter) account timelines via Nitter RSS.
    - `search` is keyword-search on Reddit (via injected `buzz_search_backend`).
      X public search is blocked / behind login and the searches users actually
      ask for ("Trump", "amd") return better discussion on Reddit anyway.
    """

    _COOLDOWN_SECONDS = 600.0
    _RATE_LIMIT_BACKOFF = 900.0

    def __init__(
        self,
        *,
        buzz_search_backend=None,
        cookies_file: str | Path = "cookies.json",
        language: str = "ja",
    ) -> None:
        self._buzz_search_backend = buzz_search_backend
        self._cookies_file = Path(cookies_file)
        self._language = language
        self._disabled_until: float = 0.0
        self._lock = asyncio.Lock()
        self._working_host: Optional[str] = None

    async def ensure_logged_in(self) -> None:
        """Nitter requires no login. Probe mirrors to find a working one."""
        async with self._lock:
            if self._working_host is not None:
                return

            logger.info("Probing Nitter mirrors for a working host...")
            loop = asyncio.get_running_loop()
            for host in NITTER_HOSTS:
                try:
                    url = f"https://{host}/aka_claw/rss"
                    body = await loop.run_in_executor(None, _fetch, url, 10.0)
                    if "<rss" in body and "<item>" in body:
                        self._working_host = host
                        logger.info("✅ Using Nitter host: %s", host)
                        return
                    logger.debug("Host %s returned no items", host)
                except Exception as e:
                    logger.debug("Host %s unreachable: %s", host, e)

            raise RuntimeError("No working Nitter mirror found")

    async def _fetch_with_fallback(self, path: str) -> str:
        """Fetch a path from the current host, falling back if it fails."""
        loop = asyncio.get_running_loop()
        hosts = ([self._working_host] if self._working_host else []) + [
            h for h in NITTER_HOSTS if h != self._working_host
        ]
        last_err: Exception | None = None
        for host in hosts:
            try:
                url = f"https://{host}{path}"
                body = await loop.run_in_executor(None, _fetch, url, 15.0)
                if self._working_host != host:
                    logger.info("Switched Nitter host to: %s", host)
                    self._working_host = host
                return body
            except Exception as e:
                last_err = e
                logger.debug("Fetch %s failed: %s", url, e)
        raise last_err or RuntimeError("All Nitter hosts failed")

    def _parse_rss(self, xml_text: str, default_handle: str = "") -> list[Tweet]:
        """Parse Nitter RSS feed into Tweet objects."""
        tweets: list[Tweet] = []
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as e:
            logger.warning("RSS parse failed: %s", e)
            return tweets

        ns = {"dc": "http://purl.org/dc/elements/1.1/"}
        for item in root.iter("item"):
            try:
                title_el = item.find("title")
                desc_el = item.find("description")
                link_el = item.find("link")
                guid_el = item.find("guid")
                pub_el = item.find("pubDate")
                creator_el = item.find("dc:creator", ns)

                text = (title_el.text or "") if title_el is not None else ""
                if desc_el is not None and desc_el.text:
                    cleaned = re.sub(r"<[^>]+>", "", desc_el.text).strip()
                    if cleaned and len(cleaned) > len(text):
                        text = cleaned

                handle = (creator_el.text.lstrip("@") if creator_el is not None and creator_el.text
                          else default_handle)

                tweet_id = "0"
                if guid_el is not None and guid_el.text:
                    tweet_id = guid_el.text.strip()
                link = link_el.text if link_el is not None and link_el.text else ""
                if tweet_id == "0" and link:
                    m = re.search(r"/status/(\d+)", link)
                    if m:
                        tweet_id = m.group(1)

                created_at = datetime.now(timezone.utc)
                if pub_el is not None and pub_el.text:
                    try:
                        parsed = parsedate_to_datetime(pub_el.text)
                        if parsed.tzinfo is None:
                            parsed = parsed.replace(tzinfo=timezone.utc)
                        created_at = parsed
                    except Exception:
                        pass

                tweets.append(Tweet(
                    tweet_id=tweet_id,
                    author_handle=handle,
                    author_id="",
                    text=text,
                    created_at=created_at,
                    lang=None,
                    retweet_count=0,
                    like_count=0,
                    url=f"https://x.com/{handle}/status/{tweet_id}" if tweet_id != "0" else link,
                ))
            except Exception as e:
                logger.debug("Failed to parse RSS item: %s", e)
        return tweets

    async def get_timeline(self, user_id: str, *, count: int = 20) -> list[Tweet]:
        """Fetch a user's timeline via Nitter RSS. user_id is screen_name."""
        if self._is_disabled():
            return []

        try:
            await self.ensure_logged_in()
            screen_name = user_id.lstrip("@")
            logger.info("Fetching timeline for @%s via Nitter", screen_name)
            xml_text = await self._fetch_with_fallback(f"/{screen_name}/rss")
            tweets = self._parse_rss(xml_text, default_handle=screen_name)
            logger.info("Found %d tweets for @%s", len(tweets), screen_name)
            await _human_delay()
            return tweets[:count]
        except Exception:
            logger.exception("XClientWeb.get_timeline failed user_id=%s", user_id)
            self._trip_circuit(self._COOLDOWN_SECONDS)
            return []

    async def resolve_user_id(self, screen_name: str) -> str:
        """Nitter uses screen_name directly; just return it cleaned."""
        return screen_name.lstrip("@")

    async def search(self, query: str, *, count: int = 15) -> list[Tweet]:
        """Keyword buzz search (currently backed by Reddit via injected backend)."""
        if self._is_disabled():
            return []
        if self._buzz_search_backend is None:
            logger.warning("XClientWeb.search: no buzz_search_backend configured")
            return []
        try:
            posts = await self._buzz_search_backend.search(query, count=count)
            posts = sorted(posts, key=lambda t: t.like_count, reverse=True)
            return posts[:count]
        except Exception:
            logger.exception("XClientWeb.search failed query=%s", query)
            self._trip_circuit(self._COOLDOWN_SECONDS)
            return []

    async def get_trends(self, category: str = "trending", *, count: int = 20) -> list[str]:
        """Nitter doesn't reliably expose trends; return empty list."""
        if self._is_disabled():
            return []
        logger.debug("get_trends: Nitter has no reliable trends endpoint, returning empty")
        return []

    async def close(self) -> None:
        """Nothing to clean up."""
        return None

    def _is_disabled(self) -> bool:
        return time.monotonic() < self._disabled_until

    def _trip_circuit(self, cooldown: float) -> None:
        self._disabled_until = time.monotonic() + cooldown
        logger.warning("XClientWeb circuit tripped cooldown_seconds=%.0f", cooldown)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

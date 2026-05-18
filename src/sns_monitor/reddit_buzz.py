"""Reddit-based buzz search.

Reddit exposes a public JSON API that requires no auth and is generous with
rate limits. We use it to power `/snsbuzz <keyword>`:

  https://www.reddit.com/search.json?q=<query>&sort=top&t=<window>&limit=N

Returns the top-scored posts in a given time window. The resulting Tweet
objects reuse our existing dataclass — author_handle holds "u/{author}",
text holds title + body, like_count holds the post score.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import subprocess
import urllib.parse
from datetime import datetime, timezone
from typing import Optional

from .models import Tweet

logger = logging.getLogger(__name__)

_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


def _curl_get(url: str, *, timeout: float = 12.0) -> Optional[str]:
    """Reddit blocks urllib by TLS fingerprint but accepts curl. Use curl."""
    curl_bin = shutil.which("curl") or "/usr/bin/curl"
    try:
        result = subprocess.run(
            [
                curl_bin, "-sSL",
                "--max-time", str(int(timeout)),
                "-H", f"User-Agent: {_USER_AGENT}",
                "-H", "Accept: application/json",
                url,
            ],
            capture_output=True,
            timeout=timeout + 5,
            check=False,
        )
        if result.returncode != 0:
            logger.warning("curl returned %d for %s", result.returncode, url)
            return None
        return result.stdout.decode("utf-8", errors="replace")
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as e:
        logger.warning("curl exec failed: %s", e)
        return None


def _reddit_search_sync(query: str, count: int, window: str = "week") -> list[Tweet]:
    """Blocking Reddit search. Returns posts sorted by score, newest first within window."""
    encoded = urllib.parse.quote(query)
    url = (
        f"https://www.reddit.com/search.json"
        f"?q={encoded}&sort=top&t={window}&limit={min(50, max(5, count))}"
        f"&restrict_sr=&include_over_18=on"
    )
    body = _curl_get(url)
    if body is None:
        return []

    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return []

    children = (data.get("data") or {}).get("children") or []
    posts: list[Tweet] = []
    for c in children:
        pd = c.get("data") or {}
        post = _parse_reddit_post(pd)
        if post is not None:
            posts.append(post)
    return posts


def _parse_reddit_post(pd: dict) -> Optional[Tweet]:
    post_id = pd.get("id") or ""
    if not post_id:
        return None
    title = (pd.get("title") or "").strip()
    selftext = (pd.get("selftext") or "").strip()
    text = title
    if selftext:
        # Trim long bodies; LLM doesn't need walls of text per post
        snippet = selftext[:600] + ("…" if len(selftext) > 600 else "")
        text = f"{title}\n\n{snippet}"

    subreddit = pd.get("subreddit") or ""
    author = pd.get("author") or "deleted"
    handle = f"r/{subreddit}"  # what we display as the "source channel"
    score = int(pd.get("score") or 0)
    num_comments = int(pd.get("num_comments") or 0)

    created_utc = pd.get("created_utc")
    if created_utc:
        try:
            created_at = datetime.fromtimestamp(float(created_utc), tz=timezone.utc)
        except (ValueError, OSError):
            created_at = datetime.now(timezone.utc)
    else:
        created_at = datetime.now(timezone.utc)

    permalink = pd.get("permalink") or ""
    url = f"https://reddit.com{permalink}" if permalink else (pd.get("url") or "")

    return Tweet(
        tweet_id=post_id,
        author_handle=handle,
        author_id=author,  # store u/author here; format_buzz_reply will surface it
        text=text,
        created_at=created_at,
        lang=None,
        retweet_count=num_comments,
        like_count=score,
        url=url,
    )


def _reddit_subreddit_hot_sync(name: str, count: int) -> list[Tweet]:
    """Blocking fetch of /r/<name>/hot.json. Returns up to count posts as Tweet."""
    cleaned = name.strip().lstrip("/").removeprefix("r/").strip("/")
    if not cleaned:
        return []
    limit = min(50, max(5, count))
    url = f"https://www.reddit.com/r/{urllib.parse.quote(cleaned, safe='')}/hot.json?limit={limit}&raw_json=1"
    body = _curl_get(url)
    if body is None:
        return []
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return []
    children = (data.get("data") or {}).get("children") or []
    posts: list[Tweet] = []
    for c in children:
        pd = c.get("data") or {}
        if pd.get("stickied"):
            # Skip pinned mod posts — they are usually rules/megathreads
            # and would dominate every poll with no new signal.
            continue
        post = _parse_reddit_post(pd)
        if post is not None:
            posts.append(post)
    return posts


class RedditBuzzClient:
    """Async wrapper around the Reddit search JSON API."""

    def __init__(self, *, default_window: str = "week") -> None:
        self._default_window = default_window

    async def search(self, query: str, *, count: int = 15, window: str | None = None) -> list[Tweet]:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None,
            _reddit_search_sync,
            query,
            count,
            window or self._default_window,
        )

    async def fetch_subreddit_hot(self, name: str, *, count: int = 25) -> list[Tweet]:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None,
            _reddit_subreddit_hot_sync,
            name,
            count,
        )

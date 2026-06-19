"""4chan-based buzz + IP-heat search.

Replaces the old Reddit JSON backend for ``/snsbuzz`` (Reddit now blocks
unauthenticated access and gates app registration behind its Responsible
Builder Policy). 4chan exposes a fully open, no-auth, read-only JSON API at
``a.4cdn.org``. We pull board *catalogs* (every live thread on a board) for a
curated set of collectible / IP boards and keyword-filter them locally — the
catalog endpoints are static, so no search API and no credentials are needed.

The point of this source is **IP heat / 風向**, not a faithful thread dump:
4chan is noisy and English/Western-otaku skewed, so the caller distills a heat
conclusion rather than relaying raw posts.

RATE LIMITING IS A HARD REQUIREMENT. 4chan's API rules: at most one request
per second and one connection at a time. We enforce a *process-global* >=1.1s
spacing under a single lock (so every 4chan request is serialised), send a
descriptive User-Agent, and cache each board catalog for 10 minutes so repeat
queries don't re-hit the API. This keeps us comfortably inside 4chan's limits
and avoids any ban risk — priority ②(不被封鎖) over ④(速度).
"""

from __future__ import annotations

import asyncio
import html
import json
import logging
import re
import threading
import time
import unicodedata
import urllib.request
from collections.abc import Sequence
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError

from .models import Tweet

logger = logging.getLogger(__name__)

# Curated collectible / IP boards (board code → what it covers):
#   vp  Pokémon (cards heavy)   toy Toys/figures/collectibles
#   tg  Traditional Games (TCG) a   Anime & Manga (IP heat)
#   co  Comics & Cartoons (Western IP)
#   vg  Vidya Game generals — where game IPs (プロセカ/PJSK, ウマ娘, FGO …) keep
#       their character/unit/event/merch chatter. Noisy board, but subject-
#       precise matching only catches the IP's own general thread.
COLLECTIBLE_BOARDS: tuple[str, ...] = ("vp", "toy", "tg", "a", "co", "vg")

_API_BASE = "https://a.4cdn.org"
_THREAD_URL = "https://boards.4chan.org/{board}/thread/{no}"
_THREAD_API = "https://a.4cdn.org/{board}/thread/{no}.json"
# Honest, descriptive UA per 4chan API etiquette (no browser impersonation).
_USER_AGENT = "openclaw-snsbuzz/0.1 (collectible IP heat monitor)"

# --- Hard rate-limit state (process-global) ---------------------------------
_MIN_INTERVAL_SECONDS = 1.1          # >= 4chan's 1 req/sec rule
_CATALOG_TTL_SECONDS = 600.0         # cache each board catalog for 10 min
_THREAD_TTL_SECONDS = 600.0          # cache each fetched thread for 10 min
_rate_lock = threading.Lock()        # serialises ALL 4chan requests
_last_request_at = 0.0
_cache_lock = threading.Lock()
_catalog_cache: dict[str, tuple[float, list[dict]]] = {}
_thread_cache: dict[tuple[str, str], tuple[float, list[dict]]] = {}

_TAG_RE = re.compile(r"<[^>]+>")


def _clean(text: str | None) -> str:
    """Strip 4chan comment HTML (<br>, quote links, …) to plain text."""
    if not text:
        return ""
    text = re.sub(r"<br\s*/?>", " ", text)
    text = _TAG_RE.sub("", text)
    return html.unescape(text).strip()


def _throttled_get(url: str, *, timeout: float = 15.0) -> str | None:
    """GET a 4chan API URL under the global 1-req/sec serialised throttle.

    The lock is held for the whole request so there is never more than one
    concurrent connection AND consecutive requests are spaced >= _MIN_INTERVAL.
    """
    global _last_request_at
    with _rate_lock:
        wait = _MIN_INTERVAL_SECONDS - (time.monotonic() - _last_request_at)
        if wait > 0:
            time.sleep(wait)
        try:
            req = urllib.request.Request(
                url,
                headers={"User-Agent": _USER_AGENT, "Accept": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except (HTTPError, URLError, TimeoutError, OSError) as exc:
            logger.warning("4chan GET failed %s: %s", url, exc)
            return None
        finally:
            _last_request_at = time.monotonic()


def _fetch_catalog(board: str) -> list[dict]:
    """Return all live threads on a board (cached 10 min). Empty list on error."""
    now = time.monotonic()
    with _cache_lock:
        hit = _catalog_cache.get(board)
        if hit is not None and now - hit[0] < _CATALOG_TTL_SECONDS:
            return hit[1]

    body = _throttled_get(f"{_API_BASE}/{board}/catalog.json")
    if body is None:
        return []
    try:
        pages = json.loads(body)
    except json.JSONDecodeError:
        logger.warning("4chan catalog for /%s/ was not JSON", board)
        return []

    threads: list[dict] = []
    for page in pages if isinstance(pages, list) else []:
        for th in page.get("threads", []) or []:
            threads.append(th)
    with _cache_lock:
        _catalog_cache[board] = (now, threads)
    return threads


def _fetch_thread(board: str, no: str) -> list[dict]:
    """Return all posts (OP first) of one thread (cached 10 min). Empty on error.

    This is the *deep* read: a thread's replies are where the concrete chatter
    lives (which card/set/character/price), as opposed to the catalog's
    board-general subject lines."""
    key = (board, str(no))
    now = time.monotonic()
    with _cache_lock:
        hit = _thread_cache.get(key)
        if hit is not None and now - hit[0] < _THREAD_TTL_SECONDS:
            return hit[1]

    body = _throttled_get(_THREAD_API.format(board=board, no=no))
    if body is None:
        return []
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        logger.warning("4chan thread /%s/%s was not JSON", board, no)
        return []
    posts = data.get("posts", []) if isinstance(data, dict) else []
    posts = [p for p in posts if isinstance(p, dict)]
    with _cache_lock:
        _thread_cache[key] = (now, posts)
    return posts


def _norm(text: str | None) -> str:
    """Lowercase + strip diacritics so 'Pokémon' matches 'pokemon'."""
    if not text:
        return ""
    decomposed = unicodedata.normalize("NFKD", text)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return stripped.lower().strip()


def _matches(query: str, sub: str, com: str) -> bool:
    """Decide whether a thread is *about* the query (not merely mentions it).

    Precision over recall: a thread counts only when the query hits the thread
    SUBJECT — the board-curated topic line, the high-signal 'this thread is
    about X' field. (e.g. a '/lg/ - LEGO General' thread that happens to mention
    'pokemon' deep in its OP must NOT inflate Pokémon heat.) Threads with no
    subject fall back to requiring the *whole* query phrase in the OP body, so
    bare general threads like 'Pokemon fucking won' still count, but scattered
    incidental term mentions do not.
    """
    q = _norm(query)
    if not q:
        return False
    terms = [t for t in re.split(r"\s+", q) if len(t) >= 2]
    nsub = _norm(sub)
    if nsub:
        if q in nsub:
            return True
        return bool(terms) and all(t in nsub for t in terms)
    # No subject → require the whole query phrase in the OP body.
    return q in _norm(com)


def _matches_any(queries: Sequence[str], sub: str, com: str) -> bool:
    """True if the thread is about *any* of the query terms.

    ``queries`` is a user term plus its known aliases (e.g. 'pjsk' →
    'Project Sekai' / 'プロセカ'). Aliases come from the caller's knowledge DB
    (RAG), never a hardcoded map — so a user who types 'pjsk' still hits a
    '/psg/ - Project SEKAI General' thread whose subject never says 'pjsk'."""
    return any(_matches(q, sub, com) for q in queries)


def _thread_to_tweet(board: str, th: dict, *, sub: str, com: str) -> Tweet:
    no = th.get("no")
    text = sub
    if com:
        snippet = com[:600] + ("…" if len(com) > 600 else "")
        text = f"{sub}\n\n{snippet}" if sub else snippet
    replies = int(th.get("replies") or 0)
    images = int(th.get("images") or 0)
    ts = th.get("time")
    if ts:
        try:
            created_at = datetime.fromtimestamp(float(ts), tz=timezone.utc)
        except (ValueError, OSError):
            created_at = datetime.now(timezone.utc)
    else:
        created_at = datetime.now(timezone.utc)
    return Tweet(
        tweet_id=str(no),
        author_handle=f"/{board}/",
        author_id="",
        text=text or "(無內文)",
        created_at=created_at,
        lang=None,
        retweet_count=images,       # secondary signal: image count
        like_count=replies,         # primary heat signal: reply count
        url=_THREAD_URL.format(board=board, no=no),
    )


def _query_terms(query: str, aliases: Sequence[str]) -> tuple[str, ...]:
    """Combine the primary query with its aliases, de-duped (case-insensitive),
    primary first. Empty terms are dropped."""
    out: list[str] = []
    seen: set[str] = set()
    for raw in (query, *aliases):
        term = (raw or "").strip()
        key = term.lower()
        if term and key not in seen:
            seen.add(key)
            out.append(term)
    return tuple(out)


def _matched_threads(
    query: str, boards: tuple[str, ...], aliases: Sequence[str] = ()
) -> list[Tweet]:
    queries = _query_terms(query, aliases)
    matched: list[Tweet] = []
    for board in boards:
        for th in _fetch_catalog(board):
            sub = _clean(th.get("sub"))
            com = _clean(th.get("com"))
            if _matches_any(queries, sub, com):
                matched.append(_thread_to_tweet(board, th, sub=sub, com=com))
    matched.sort(key=lambda t: t.like_count, reverse=True)
    return matched


def _measure_heat(
    query: str, boards: tuple[str, ...], aliases: Sequence[str] = ()
) -> tuple[float, int]:
    """Return (heat_value, matched_thread_count).

    Heat = sum of reply counts across all matched threads — captures both
    breadth (many threads) and depth (busy threads)."""
    queries = _query_terms(query, aliases)
    total = 0.0
    count = 0
    for board in boards:
        for th in _fetch_catalog(board):
            sub = _clean(th.get("sub"))
            com = _clean(th.get("com"))
            if _matches_any(queries, sub, com):
                total += float(th.get("replies") or 0)
                count += 1
    return total, count


class FourchanBuzzClient:
    """Drop-in buzz backend (replaces RedditBuzzClient).

    Implements the ``search(query, *, count, window)`` coroutine the buzz
    pipeline expects, plus ``measure_ip_heat_sync`` for IP-heat distillation.
    Board catalogs are cached process-wide, so a search immediately followed by
    a heat measurement triggers only ONE round of network fetches.
    """

    def __init__(self, *, boards: tuple[str, ...] = COLLECTIBLE_BOARDS) -> None:
        self._boards = tuple(boards)

    async def search(
        self, query: str, *, count: int = 15, window: str | None = None,
        aliases: Sequence[str] = (),
    ) -> list[Tweet]:
        # `window` is accepted for interface compat; 4chan catalogs are "live now".
        loop = asyncio.get_running_loop()
        posts = await loop.run_in_executor(
            None, _matched_threads, query, self._boards, aliases
        )
        return posts[:count] if count else posts

    def search_sync(
        self, query: str, *, count: int = 15, aliases: Sequence[str] = ()
    ) -> list[Tweet]:
        posts = _matched_threads(query, self._boards, aliases)
        return posts[:count] if count else posts

    def measure_ip_heat_sync(
        self, query: str, aliases: Sequence[str] = ()
    ) -> tuple[float, int]:
        return _measure_heat(query, self._boards, aliases)

    def deep_context(
        self, tweets: list[Tweet], *, top_n: int = 3, char_budget: int = 1600
    ) -> str:
        """Fetch the *actual discussion* (OP + replies) of the top-N busiest
        matched threads and return a labelled text blob for the distiller.

        Catalog subject lines are board-general names ('/tcgp/ - Pokémon TCG
        Pocket General'); the concrete signal — which card set / single card /
        character / unit / event / price — lives in the replies. We read only
        the top-N threads (default 3) to stay polite to 4chan's 1-req/sec rule;
        every fetch goes through the same global throttle + cache.

        ``tweets`` are expected pre-sorted by reply count (search() does this);
        each carries board in ``author_handle`` ('/vp/') and thread id in
        ``tweet_id``."""
        blocks: list[str] = []
        for t in tweets[: max(0, top_n)]:
            board = (t.author_handle or "").strip("/").strip()
            no = (t.tweet_id or "").strip()
            if not board or not no:
                continue
            posts = _fetch_thread(board, no)
            if not posts:
                continue
            sub = _clean(posts[0].get("sub"))
            bodies = [c for c in (_clean(p.get("com")) for p in posts) if c]
            joined = " / ".join(bodies)
            if len(joined) > char_budget:
                joined = joined[:char_budget] + "…"
            header = f"〔/{board}/ {sub or no}｜回覆{t.like_count}〕"
            blocks.append(f"{header}\n{joined}")
        return "\n\n".join(blocks)

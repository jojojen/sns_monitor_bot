from __future__ import annotations

import asyncio
import json
import logging
import ssl
import urllib.request
from dataclasses import dataclass
from typing import Optional
from urllib.error import HTTPError, URLError

from .models import Tweet
from .x_client_web import XClientWeb

logger = logging.getLogger(__name__)


@dataclass
class BuzzResult:
    query: str
    summary: str
    sources: list[Tweet]
    fetched_count: int


def _build_prompt(query: str, posts: list[Tweet]) -> str:
    lines = [
        f"使用者想了解 Reddit 上關於「{query}」的熱門討論。",
        f"以下是抓到的 {len(posts)} 則熱門貼文（按按讚數排序）：",
        "",
    ]
    for i, p in enumerate(posts, 1):
        text = p.text.replace("\n", " ").strip()
        if len(text) > 300:
            text = text[:300] + "…"
        author = p.author_id or "deleted"
        lines.append(
            f"[{i}] {p.author_handle} • u/{author} • ⬆️{p.like_count} 💬{p.retweet_count} • {p.created_at:%Y-%m-%d}: {text}"
        )
    lines.extend([
        "",
        "請完成兩件事：",
        "1. 用 3-5 句繁體中文摘要這些貼文反映出的主要觀點、事件、或情緒，要點出幾個 subreddit 的不同角度",
        "2. 從上方貼文中挑出 3-5 則最有代表性或最有資訊量的，只列出編號",
        "",
        "請嚴格以 JSON 回覆，不要 markdown 程式碼框：",
        '{"summary": "...", "picks": [編號1, 編號2, ...]}',
    ])
    return "\n".join(lines)


def _resolve_generate_url(endpoint: str) -> str:
    e = endpoint.rstrip("/")
    return e if e.endswith("/api/generate") else f"{e}/api/generate"


def _call_ollama(
    endpoint: str,
    model: str,
    prompt: str,
    *,
    timeout: int = 75,
    ssl_context: Optional[ssl.SSLContext] = None,
) -> str:
    payload = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "format": "json",
    }
    req = urllib.request.Request(
        _resolve_generate_url(endpoint),
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout, context=ssl_context) as resp:
        body = resp.read().decode("utf-8", errors="replace")
    data = json.loads(body)
    response = data.get("response", "")
    if isinstance(response, dict):
        return json.dumps(response, ensure_ascii=False)
    return str(response).strip()


def _parse_llm_response(text: str, tweets: list[Tweet]) -> tuple[str, list[Tweet]]:
    """Parse LLM JSON response. Falls back gracefully if parsing fails."""
    try:
        # Strip optional code-fence wrappers
        cleaned = text.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.strip("`")
            if cleaned.lower().startswith("json"):
                cleaned = cleaned[4:].strip()
        data = json.loads(cleaned)
        summary = str(data.get("summary", "")).strip() or "(LLM 未能生成摘要)"
        picks_raw = data.get("picks", [])
        picks: list[Tweet] = []
        for n in picks_raw:
            try:
                idx = int(n) - 1
                if 0 <= idx < len(tweets):
                    picks.append(tweets[idx])
            except (TypeError, ValueError):
                continue
        if not picks:
            picks = tweets[:3]
        return summary, picks
    except (json.JSONDecodeError, ValueError) as e:
        logger.warning("LLM response not valid JSON, using raw text: %s", e)
        return text.strip() or "(LLM 未能生成摘要)", tweets[:3]


async def _gather_tweets(query: str, x_client: XClientWeb, max_tweets: int) -> tuple[list[Tweet], str]:
    """Gather buzz posts for a keyword.

    @user → that user's X timeline via Nitter.
    Otherwise → Reddit keyword search.
    """
    cleaned = query.strip()
    if cleaned.startswith("@"):
        handle = cleaned.lstrip("@")
        tweets = await x_client.get_timeline(handle, count=max_tweets)
        return tweets, f"@{handle} (X)"

    posts = await x_client.search(cleaned, count=max_tweets)
    return posts, f"關鍵字「{cleaned}」(Reddit)"


async def summarize_topic(
    query: str,
    *,
    x_client: XClientWeb,
    llm_endpoint: str,
    llm_model: str,
    llm_timeout: int = 75,
    ssl_context: Optional[ssl.SSLContext] = None,
    max_tweets: int = 30,
) -> Optional[BuzzResult]:
    """Search X for a topic, summarize via LLM, return summary + source tweets."""
    tweets, source_label = await _gather_tweets(query, x_client, max_tweets)
    if not tweets:
        return None

    prompt = _build_prompt(source_label, tweets)
    loop = asyncio.get_running_loop()
    try:
        raw = await loop.run_in_executor(
            None,
            lambda: _call_ollama(llm_endpoint, llm_model, prompt,
                                 timeout=llm_timeout, ssl_context=ssl_context),
        )
    except (HTTPError, URLError, TimeoutError, OSError) as e:
        logger.exception("LLM call failed for query=%s: %s", query, e)
        # Graceful fallback: skip LLM, show raw tweets
        return BuzzResult(
            query=source_label,
            summary=f"(LLM 暫時無法使用，以下是最新 {min(5, len(tweets))} 則推文)",
            sources=tweets[:5],
            fetched_count=len(tweets),
        )

    summary, picks = _parse_llm_response(raw, tweets)
    return BuzzResult(
        query=source_label,
        summary=summary,
        sources=picks,
        fetched_count=len(tweets),
    )


def summarize_topic_sync(
    query: str,
    *,
    x_client: XClientWeb,
    llm_endpoint: str,
    llm_model: str,
    llm_timeout: int = 75,
    ssl_context: Optional[ssl.SSLContext] = None,
    max_tweets: int = 30,
) -> Optional[BuzzResult]:
    """Blocking wrapper for Telegram command processor."""
    return asyncio.run(summarize_topic(
        query,
        x_client=x_client,
        llm_endpoint=llm_endpoint,
        llm_model=llm_model,
        llm_timeout=llm_timeout,
        ssl_context=ssl_context,
        max_tweets=max_tweets,
    ))


def format_buzz_reply(result: BuzzResult) -> str:
    """Format BuzzResult as a Telegram message."""
    lines = [
        f"🔥 熱門整理：{result.query}",
        f"（共抓取 {result.fetched_count} 則貼文，依按讚數排序）",
        "",
        result.summary,
        "",
        "📌 來源：",
    ]
    for p in result.sources:
        snippet = p.text.replace("\n", " ").strip()
        if len(snippet) > 90:
            snippet = snippet[:90] + "…"
        metrics_bits = []
        if p.like_count:
            metrics_bits.append(f"⬆️{p.like_count}")
        if p.retweet_count:
            metrics_bits.append(f"💬{p.retweet_count}")
        metrics = (" " + " ".join(metrics_bits)) if metrics_bits else ""
        author_bit = p.author_handle
        if p.author_id and p.author_handle.startswith("r/"):
            author_bit = f"{p.author_handle} · u/{p.author_id}"
        lines.append(f"• {author_bit}{metrics}: {snippet}")
        lines.append(f"  {p.url}")
    return "\n".join(lines)

from __future__ import annotations

import asyncio
import json
import logging
import ssl
import urllib.request
from dataclasses import dataclass
from typing import Callable, Optional
from urllib.error import HTTPError, URLError

from .models import Tweet
from .x_client_web import XClientWeb

logger = logging.getLogger(__name__)


_TARGET_CATEGORIES = {
    "group",
    "character",
    "event",
    "gacha",
    "card",
    "card_box",
    "product",
    "other",
}

_TARGET_LABELS = {
    "group": "團體",
    "character": "角色",
    "event": "活動",
    "gacha": "卡池",
    "card": "單卡",
    "card_box": "卡盒",
    "product": "商品",
    "other": "其他",
}


@dataclass(frozen=True)
class BuzzTarget:
    category: str
    name: str
    reason: str = ""
    evidence: str = ""
    confidence: int = 0


@dataclass
class BuzzResult:
    query: str
    summary: str
    sources: list[Tweet]
    fetched_count: int
    hot_items: tuple[str, ...] = ()      # specific products/sets/characters in play
    catalyst: str = ""                    # concrete event (new set/restock/price/event); "" if none
    actionable: str = ""                  # concrete watch/buy target; "" if none
    collectible_signal: str = "low"       # "high" | "medium" | "low"
    targets: tuple[BuzzTarget, ...] = ()   # categorized concrete targets

    @property
    def has_signal(self) -> bool:
        """True when there is something worth persisting to the knowledge base.

        A bare 'lots of chatter, no specific product or catalyst' scan is NOT a
        signal — persisting it would just relaunder the vague noise the user
        complained about. We require a concrete product, a catalyst event, or an
        explicit high collectible signal."""
        return (
            bool(self.catalyst)
            or bool(self.hot_items)
            or bool(self.targets)
            or self.collectible_signal == "high"
        )


def _build_prompt(
    query: str,
    posts: list[Tweet],
    *,
    deep_context: str = "",
    entity_context: str = "",
) -> str:
    lines = [
        f"使用者要從 4chan 收藏品/IP 板的討論，判讀「{query}」的『收藏品買賣價值訊號』。",
        f"以下是命中的 {len(posts)} 則討論串（依回覆數排序，回覆數＝熱度）：",
        "",
    ]
    for i, p in enumerate(posts, 1):
        text = p.text.replace("\n", " ").strip()
        if len(text) > 300:
            text = text[:300] + "…"
        lines.append(
            f"[{i}] {p.author_handle} • 💬{p.like_count}回覆 🖼{p.retweet_count} • {p.created_at:%Y-%m-%d}: {text}"
        )
    if deep_context:
        lines.extend([
            "",
            "【最熱串的實際討論內容（OP＋回覆節錄）——具體商品/卡/角色/價格訊號多在這裡，"
            "請以這段為主要依據】：",
            deep_context,
        ])
    if entity_context:
        lines.extend([
            "",
            "【可擴充 IP 辭典（只作正規化輔助，不可當成討論證據）】：",
            entity_context,
        ])
    lines.extend([
        "",
        "從上面（尤其『實際討論內容』）抽出『可行動的收藏訊號』。"
        "**只輸出具體專有名詞與事件，嚴禁情緒形容詞**"
        "（『熱度高』『討論熱烈』『情緒正面』這類一律不要）；"
        "**更嚴禁輸出看板名 / General 串名 / 板規**："
        "（例：『Pokémon TCG Pocket General』『/tcgp/』『Project Sekai General』這種看板層級名稱"
        "毫無收藏資訊量，一律不要）：",
        "- hot_items：被『實際討論』到的『具體』商品/卡組/單卡/角色/團體(unit)/系列/活動 專有名詞"
        "（例：『テラスタル フェスティバル』『リザードン ex SAR』『Leo/need』『25時、ナイトコードで。』"
        "『限定アクスタ』『64パック箱』）。"
        "沒有具體專有名詞就空陣列 []，不要用看板名硬湊。",
        "- targets：把 hot_items 拆成分類物件。category 僅能是 "
        "group/character/event/gacha/card/card_box/product/other。"
        "name 必須是實際討論內容出現的標的，或由 IP 辭典正規化後的同一標的；"
        "reason 寫為何有收藏/入手機會；evidence 放 4chan 內容中的短證據；confidence 0-100。"
        "沒有具體標的就 []。",
        "- catalyst：推動這波討論的『具體事件』——新彈上市/復刻/漲價/缺貨/聯名/活動/新作發表/再販 等。"
        "沒有明確事件就填 null（不要硬掰）。",
        "- actionable：從收藏或轉售角度，『具體』值得關注或入手的標的，並簡述為何可能增值"
        "（稀有度/缺貨/聯名/價格趨勢）；不確定或沒有就填 null。禁止臆造。",
        "- collectible_signal：這批討論對『收藏品買賣』的價值高低 high/medium/low"
        "（多為對戰攻略/二創/迷因/閒聊 → low；有具體商品或交易/價格討論 → medium/high）。",
        "- picks：最能佐證的 3-5 則編號。",
        "",
        "嚴格以 JSON 回覆，不要 markdown 程式碼框：",
        '{"hot_items": [...], "catalyst": "... 或 null", "actionable": "... 或 null", '
        '"collectible_signal": "high|medium|low", '
        '"targets": [{"category": "group|character|event|gacha|card|card_box|product|other", '
        '"name": "...", "reason": "...", "evidence": "...", "confidence": 0-100}], '
        '"picks": [編號, ...]}',
    ])
    return "\n".join(lines)


_SIGNAL_LABELS = {"high": "高", "medium": "中", "low": "低"}


def _format_targets(targets: tuple[BuzzTarget, ...]) -> str:
    if not targets:
        return ""
    grouped: dict[str, list[str]] = {}
    for target in targets:
        label = _TARGET_LABELS.get(target.category, "其他")
        if target.name not in grouped.setdefault(label, []):
            grouped[label].append(target.name)
    return "；".join(f"{label}：" + "、".join(names) for label, names in grouped.items())


def _compose_summary(
    hot_items: tuple[str, ...],
    catalyst: str,
    actionable: str,
    signal: str,
    targets: tuple[BuzzTarget, ...] = (),
) -> str:
    """Build the human-facing conclusion from structured fields — deterministic,
    noun-driven, no LLM sentiment filler."""
    sig_label = _SIGNAL_LABELS.get(signal, "低")
    if not hot_items and not catalyst and not actionable and not targets:
        return f"多為一般討論，無明確收藏催化或入手標的（收藏訊號：{sig_label}）。"
    parts: list[str] = []
    target_text = _format_targets(targets)
    if target_text:
        parts.append("具體標的：" + target_text)
    if hot_items:
        parts.append("熱門標的：" + "、".join(hot_items))
    if catalyst:
        parts.append("催化：" + catalyst)
    if actionable:
        parts.append("可留意：" + actionable)
    parts.append("收藏訊號：" + sig_label)
    return "。".join(parts) + "。"


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
        # qwen3-class models default to thinking mode; combined with format=json
        # they spend the token budget on a <think> block and return an empty
        # "{}" response. Disable thinking so the JSON summary is actually emitted.
        "think": False,
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


def _coerce_optional(value: object) -> str:
    """Normalize an optional LLM string field: None / 'null' / '' → ''."""
    if not isinstance(value, str):
        return ""
    cleaned = value.strip()
    if not cleaned or cleaned.lower() == "null":
        return ""
    return cleaned


@dataclass
class _ParsedBuzz:
    hot_items: tuple[str, ...]
    catalyst: str
    actionable: str
    collectible_signal: str
    targets: tuple[BuzzTarget, ...]
    picks: list[Tweet]


def _coerce_target(raw: object) -> BuzzTarget | None:
    if not isinstance(raw, dict):
        return None
    name = _coerce_optional(raw.get("name"))
    if not name:
        return None
    category = _coerce_optional(raw.get("category")).lower()
    if category not in _TARGET_CATEGORIES:
        category = "other"
    try:
        confidence = int(raw.get("confidence") or 0)
    except (TypeError, ValueError):
        confidence = 0
    confidence = max(0, min(100, confidence))
    return BuzzTarget(
        category=category,
        name=name[:120],
        reason=_coerce_optional(raw.get("reason"))[:240],
        evidence=_coerce_optional(raw.get("evidence"))[:240],
        confidence=confidence,
    )


def _parse_llm_response(text: str, tweets: list[Tweet]) -> _ParsedBuzz:
    """Parse the structured collectible-signal JSON. Falls back to an empty
    (no-signal) result if parsing fails, so a malformed LLM reply can never
    fabricate a signal."""
    try:
        cleaned = text.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.strip("`")
            if cleaned.lower().startswith("json"):
                cleaned = cleaned[4:].strip()
        data = json.loads(cleaned)
        if not isinstance(data, dict):
            raise ValueError("LLM JSON was not an object")

        hot_raw = data.get("hot_items") or []
        if not isinstance(hot_raw, list):
            hot_raw = []
        hot_items = tuple(
            s for s in (str(x).strip() for x in hot_raw) if s and s.lower() != "null"
        )[:8]

        targets_raw = data.get("targets") or []
        if not isinstance(targets_raw, list):
            targets_raw = []
        targets_list: list[BuzzTarget] = []
        seen_targets: set[tuple[str, str]] = set()
        for raw_target in targets_raw:
            target = _coerce_target(raw_target)
            if target is None:
                continue
            key = (target.category, target.name.casefold())
            if key in seen_targets:
                continue
            seen_targets.add(key)
            targets_list.append(target)
            if len(targets_list) >= 12:
                break
        targets = tuple(targets_list)

        catalyst = _coerce_optional(data.get("catalyst"))
        actionable = _coerce_optional(data.get("actionable"))
        signal = str(data.get("collectible_signal") or "low").strip().lower()
        if signal not in _SIGNAL_LABELS:
            signal = "low"

        picks: list[Tweet] = []
        for n in data.get("picks", []) or []:
            try:
                idx = int(n) - 1
            except (TypeError, ValueError):
                continue
            if 0 <= idx < len(tweets):
                picks.append(tweets[idx])
        if not picks:
            picks = tweets[:3]

        return _ParsedBuzz(hot_items, catalyst, actionable, signal, targets, picks)
    except (json.JSONDecodeError, ValueError, TypeError) as e:
        logger.warning("Buzz LLM response not parseable, treating as no-signal: %s", e)
        return _ParsedBuzz((), "", "", "low", (), tweets[:3])


async def _gather_tweets(
    query: str, x_client: XClientWeb, max_tweets: int, search_aliases: tuple[str, ...] = (),
) -> tuple[list[Tweet], str]:
    """Gather buzz posts for a keyword.

    @user → that user's X timeline via Nitter.
    Otherwise → 4chan keyword search (collectible / IP boards). ``search_aliases``
    are caller-resolved alternative names (RAG) so the search matches a thread
    about the query *or* any alias.
    """
    cleaned = query.strip()
    if cleaned.startswith("@"):
        handle = cleaned.lstrip("@")
        tweets = await x_client.get_timeline(handle, count=max_tweets)
        return tweets, f"@{handle} (X)"

    posts = await x_client.search(cleaned, count=max_tweets, aliases=search_aliases)
    return posts, f"關鍵字「{cleaned}」(4chan)"


async def summarize_topic(
    query: str,
    *,
    x_client: XClientWeb,
    llm_endpoint: str,
    llm_model: str,
    llm_timeout: int = 75,
    ssl_context: Optional[ssl.SSLContext] = None,
    max_tweets: int = 30,
    llm_call_fn: Optional[Callable[[str], str]] = None,
    deep_context_fn: Optional[Callable[[list[Tweet]], str]] = None,
    entity_context_fn: Optional[Callable[[str, tuple[str, ...], list[Tweet], str], str]] = None,
    search_aliases: tuple[str, ...] = (),
) -> Optional[BuzzResult]:
    """Search X for a topic, summarize via LLM, return summary + source tweets.

    ``deep_context_fn`` (optional) fetches the actual discussion (OP+replies) of
    the busiest threads so the distiller sees concrete product/card/character
    chatter, not just board-general subject lines. ``llm_call_fn`` (optional)
    overrides the LLM call — e.g. to run the abstract distillation on cloud
    big-pickle with a local fallback. ``search_aliases`` are caller-resolved
    alternative names (RAG) widening the 4chan match. All default to the prior
    local behaviour.
    """
    tweets, source_label = await _gather_tweets(query, x_client, max_tweets, search_aliases)
    if not tweets:
        return None

    loop = asyncio.get_running_loop()

    deep_context = ""
    if deep_context_fn is not None:
        try:
            deep_context = await loop.run_in_executor(
                None, lambda: deep_context_fn(tweets) or ""
            )
        except Exception:
            logger.exception("Deep-context fetch failed for query=%s", query)
            deep_context = ""

    entity_context = ""
    if entity_context_fn is not None:
        try:
            entity_context = await loop.run_in_executor(
                None,
                lambda: entity_context_fn(query, search_aliases, tweets, deep_context) or "",
            )
        except Exception:
            logger.exception("Entity-context build failed for query=%s", query)
            entity_context = ""

    prompt = _build_prompt(
        source_label,
        tweets,
        deep_context=deep_context,
        entity_context=entity_context,
    )
    call = llm_call_fn or (
        lambda p: _call_ollama(llm_endpoint, llm_model, p,
                               timeout=llm_timeout, ssl_context=ssl_context)
    )
    try:
        raw = await loop.run_in_executor(None, lambda: call(prompt))
    except (HTTPError, URLError, TimeoutError, OSError) as e:
        logger.exception("LLM call failed for query=%s: %s", query, e)
        # Graceful fallback: skip LLM, show raw threads. No signal is claimed,
        # so nothing gets persisted to the knowledge base on an LLM outage.
        return BuzzResult(
            query=source_label,
            summary=f"(LLM 暫時無法使用，以下是最新 {min(5, len(tweets))} 則討論串)",
            sources=tweets[:5],
            fetched_count=len(tweets),
        )

    parsed = _parse_llm_response(raw, tweets)
    summary = _compose_summary(
        parsed.hot_items,
        parsed.catalyst,
        parsed.actionable,
        parsed.collectible_signal,
        parsed.targets,
    )
    return BuzzResult(
        query=source_label,
        summary=summary,
        sources=parsed.picks,
        fetched_count=len(tweets),
        hot_items=parsed.hot_items,
        catalyst=parsed.catalyst,
        actionable=parsed.actionable,
        collectible_signal=parsed.collectible_signal,
        targets=parsed.targets,
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
    llm_call_fn: Optional[Callable[[str], str]] = None,
    deep_context_fn: Optional[Callable[[list[Tweet]], str]] = None,
    entity_context_fn: Optional[Callable[[str, tuple[str, ...], list[Tweet], str], str]] = None,
    search_aliases: tuple[str, ...] = (),
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
        llm_call_fn=llm_call_fn,
        deep_context_fn=deep_context_fn,
        entity_context_fn=entity_context_fn,
        search_aliases=search_aliases,
    ))


def format_buzz_reply(result: BuzzResult) -> str:
    """Format BuzzResult as a Telegram message."""
    if not result.has_signal:
        return "\n".join([
            f"🔥 熱門整理：{result.query}",
            f"（共抓取 {result.fetched_count} 則討論串，依回覆數排序）",
            "",
            result.summary,
            "",
            "目前沒有抽到具體收藏催化或入手標的；不列來源串，避免把一般閒聊誤當新知。",
        ])

    lines = [
        f"🔥 熱門整理：{result.query}",
        f"（共抓取 {result.fetched_count} 則討論串，依回覆數排序）",
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
            metrics_bits.append(f"💬{p.like_count}")
        if p.retweet_count:
            metrics_bits.append(f"🖼{p.retweet_count}")
        metrics = (" " + " ".join(metrics_bits)) if metrics_bits else ""
        lines.append(f"• {p.author_handle}{metrics}: {snippet}")
        lines.append(f"  {p.url}")
    return "\n".join(lines)

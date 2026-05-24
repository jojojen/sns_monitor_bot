"""LLM-as-judge classifier mapping a single SNS tweet to two opportunity
signals: 📈 long-term investment and ⚡ immediate arbitrage.

Run order in the monitor:
  entity_extractor.extract_entities() → knowledge_db.retrieve summaries →
  interest_profile.build_user_interest_profile() → classify_sns_signal() →
  storage.record_sns_signal() → (bypass A check + score gate) → notify

The classifier is the prompt + JSON parser. It does NOT touch the DB itself
(caller writes the result). On any failure (LLM error / non-JSON response)
the fallback returns conservative zero-scores so the gate drops the tweet
rather than spamming.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Callable, Mapping, Sequence

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SnsPostSignal:
    tweet_id: str
    rule_id: str
    long_term_score: int            # 📈 future-value signal strength (0-100)
    arbitrage_score: int            # ⚡ immediate-action signal strength (0-100)
    matched_products: tuple[str, ...]   # products from user profile that the tweet mentions
    matched_keywords: tuple[str, ...]   # general TCG keywords ("抽選" / "再販" / "予約")
    matched_entities: tuple[str, ...]   # canonical names from knowledge base
    suggested_action: str               # one-line imperative
    rationale: str                      # ≤ 60-char "why this score"
    deadline_iso: str | None            # ISO8601 if extractable, else None


# Score thresholds — kept here so tests can override without re-deploying.
DEFAULT_MIN_SCORE_TO_PUSH: int = 60


def build_classifier_prompt(
    *,
    tweet_id: str,
    author_handle: str,
    created_at: str,
    tweet_text: str,
    watchlist_queries: Sequence[str],
    pinned_targets: Sequence[str],
    feedback_for_rule: Mapping[str, int],
    knowledge_block: str,
) -> str:
    """Compose the LLM prompt. Public for testability — assert against
    rendered text in unit tests."""
    def _list_or_none(items: Sequence[str], limit: int = 30) -> str:
        if not items:
            return "（無）"
        shown = list(items)[:limit]
        suffix = "…" if len(items) > limit else ""
        return "、".join(shown) + suffix

    fb = feedback_for_rule or {}
    up = int(fb.get("up", 0))
    bought = int(fb.get("bought", 0))
    down = int(fb.get("down", 0))

    return (
        "你是 TCG 收藏品商機分類器。給一則 SNS 推文 + 使用者目前的興趣檔案，\n"
        "判斷這則推文是否帶有以下兩種訊號之一或兩者：\n"
        "\n"
        "1. 📈 長期潛力訊號：商品因 EOL / 供給縮減 / 收藏熱度上升 / 新弾發表等理由，\n"
        "   未來價格有上漲潛力。現在以合理價購入屬於長期投資。\n"
        "2. ⚡ 立即套利訊號：restock / 抽選開放 / 限時優惠 / mispriced listing /\n"
        "   deadline 提醒 — 立即行動可撿到明顯低於市價的商品。\n"
        "\n"
        "使用者興趣檔案：\n"
        f"- 主動追蹤的商品（Mercari/Rakuma watchlist）：{_list_or_none(watchlist_queries)}\n"
        f"- 釘選為目標的 candidate：{_list_or_none(pinned_targets)}\n"
        "- 過去 30 天 SNS feedback 統計（按 rule 聚合）：\n"
        f"    👍 過此 rule 的次數：{up}\n"
        f"    💰 因此 rule 下手過：{bought}\n"
        f"    👎 過此 rule 的次數：{down}\n"
        "\n"
        "知識庫參考（針對推文中提到的 entity 自動 retrieve；可能含過時資訊，\n"
        "請以推文為主、知識庫為輔）：\n"
        f"{knowledge_block}\n"
        "\n"
        "推文內容：\n"
        f"作者：@{author_handle}\n"
        f"時間：{created_at}\n"
        "文字：\n"
        f'"""\n{tweet_text}\n"""\n'
        "\n"
        "請嚴格輸出 JSON（不要 markdown fences、不要說明）：\n"
        "{\n"
        '  "long_term_score": 0-100,\n'
        '  "arbitrage_score": 0-100,\n'
        '  "matched_products": ["..."],\n'
        '  "matched_keywords": ["..."],\n'
        '  "suggested_action": "一句話、imperative、繁體中文",\n'
        '  "rationale": "為何給此分數、依據哪幾個句子（≤ 60 字、繁體中文）",\n'
        '  "deadline": "ISO8601 或 null"\n'
        "}\n"
        "\n"
        "分數標準：\n"
        "- 0-29 = 雜訊 / 無關 / 二手評論\n"
        "- 30-59 = 弱訊號（提到的商品不在使用者興趣內、或行動意圖模糊）\n"
        "- 60-79 = 中訊號（商品 partially 在使用者興趣內，或行動意圖明確）\n"
        "- 80-100 = 強訊號（商品在 watchlist / pinned 內、且行動類型明確）\n"
        "\n"
        "兩個分數可同時高（例：「アビスアイ 1BOX 抽選販売」對長期+立即都是強訊號）。\n"
        "未提到使用者興趣內的商品，最高給到 50（除非為通用 TCG 大事件如 set EOL 公告）。\n"
        "\n"
        "**Ex-ante 事前訊號特別加權**（這些是高優先級「未來機會」訊號，即使商品還沒在使用者 watchlist）：\n"
        "- 「IP × TCG collab 公告」(例：『チェンソーマン × UNION ARENA』『鬼滅 × Weiss』)\n"
        "    → long_term_score 70-85（未來潛力）、若同時開放予約 arbitrage_score 60-80\n"
        "- 「予約開始」/「抽選販賣公告」+ deadline（例：『6/1 10:00 抽選申込開始』）\n"
        "    → arbitrage_score 70-90（立即行動）、同時 long_term_score 60-80 若 IP 知名\n"
        "- 「アニメ第 N 期発表」/「劇場版公開」/「漫畫完結倒數」/「実写化」等 IP 熱度前置訊號\n"
        "    → long_term_score 60-80（即使尚無 TCG 公告，IP 熱度先行訊號也值得追蹤）\n"
        "- 「新弾発売決定」/「ブースター発売」/「拡張パック公開」（Bushiroad / UA / Pokemon Center 公式）\n"
        "    → long_term_score 70-85、若含發售日 arbitrage_score 50-70（為 deadline 留時間）\n"
        "\n"
        "判斷時請優先看推文是否帶以上 ex-ante 訊號 — 即使商品不在使用者 watchlist，這類訊號仍應給到 60+ 分。"
    )


_JSON_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


def _parse_classifier_response(raw: str) -> dict | None:
    """Tolerant JSON parser. Returns None on unparseable output — caller
    treats that as a conservative no-signal."""
    if not raw:
        return None
    text = _JSON_FENCE_RE.sub("", raw.strip())
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except (TypeError, ValueError, json.JSONDecodeError):
        pass
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            parsed = json.loads(match.group(0))
            if isinstance(parsed, dict):
                return parsed
        except (TypeError, ValueError, json.JSONDecodeError):
            pass
    return None


def _clamp_score(value: object) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return 0
    return max(0, min(100, n))


def _coerce_str_list(value: object, *, limit: int = 8) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    out: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            continue
        cleaned = item.strip()
        if not cleaned or cleaned.casefold() in seen:
            continue
        seen.add(cleaned.casefold())
        out.append(cleaned)
        if len(out) >= limit:
            break
    return tuple(out)


def classify_sns_signal(
    *,
    tweet_id: str,
    rule_id: str,
    author_handle: str,
    created_at: str,
    tweet_text: str,
    watchlist_queries: Sequence[str],
    pinned_targets: Sequence[str],
    feedback_for_rule: Mapping[str, int],
    knowledge_block: str,
    matched_entities: tuple[str, ...],
    llm_fn: Callable[[str], str] | None,
) -> SnsPostSignal:
    """Run the LLM judge. On any failure returns zero scores so the gate
    drops the tweet (conservative — better silent than wrong-push)."""

    if llm_fn is None:
        logger.debug(
            "classifier: no llm_fn provided — returning zero scores tweet_id=%s",
            tweet_id,
        )
        return _empty_signal(tweet_id, rule_id, matched_entities)

    prompt = build_classifier_prompt(
        tweet_id=tweet_id, author_handle=author_handle, created_at=created_at,
        tweet_text=tweet_text,
        watchlist_queries=watchlist_queries, pinned_targets=pinned_targets,
        feedback_for_rule=feedback_for_rule, knowledge_block=knowledge_block,
    )
    try:
        raw = llm_fn(prompt)
    except Exception:
        logger.exception("classifier: LLM call failed tweet_id=%s rule_id=%s",
                         tweet_id, rule_id)
        return _empty_signal(tweet_id, rule_id, matched_entities)

    parsed = _parse_classifier_response(raw)
    if parsed is None:
        logger.warning(
            "classifier: failed to parse LLM JSON tweet_id=%s rule_id=%s raw=%r",
            tweet_id, rule_id, (raw or "")[:200],
        )
        return _empty_signal(tweet_id, rule_id, matched_entities)

    deadline = parsed.get("deadline")
    if not isinstance(deadline, str) or not deadline.strip() or deadline.strip().lower() == "null":
        deadline_iso = None
    else:
        deadline_iso = deadline.strip()

    return SnsPostSignal(
        tweet_id=tweet_id,
        rule_id=rule_id,
        long_term_score=_clamp_score(parsed.get("long_term_score")),
        arbitrage_score=_clamp_score(parsed.get("arbitrage_score")),
        matched_products=_coerce_str_list(parsed.get("matched_products")),
        matched_keywords=_coerce_str_list(parsed.get("matched_keywords")),
        matched_entities=matched_entities,
        suggested_action=str(parsed.get("suggested_action", "")).strip()[:200],
        rationale=str(parsed.get("rationale", "")).strip()[:300],
        deadline_iso=deadline_iso,
    )


def _empty_signal(tweet_id: str, rule_id: str, matched_entities: tuple[str, ...]) -> SnsPostSignal:
    return SnsPostSignal(
        tweet_id=tweet_id, rule_id=rule_id,
        long_term_score=0, arbitrage_score=0,
        matched_products=(), matched_keywords=(),
        matched_entities=matched_entities,
        suggested_action="", rationale="(分類失敗)", deadline_iso=None,
    )


# ── Gate decision helpers ────────────────────────────────────────────────────


def decide_push_reason(
    *,
    signal: SnsPostSignal,
    keyword_matched: bool,
    min_score: int = DEFAULT_MIN_SCORE_TO_PUSH,
) -> str:
    """Return the bypass_reason that determines whether the monitor pushes.

    Values: 'explicit_keyword' / 'both' / 'long_term' / 'arbitrage' / 'none'.
    Caller pushes iff the return value is not 'none'.

    Bypass A: when the rule's own include_keywords matched the tweet, always
    push regardless of LLM score. This protects the user's hand-set keyword
    filters — they opted into seeing those phrases, period.
    """
    if keyword_matched:
        return "explicit_keyword"
    lt = signal.long_term_score >= min_score
    arb = signal.arbitrage_score >= min_score
    if lt and arb:
        return "both"
    if lt:
        return "long_term"
    if arb:
        return "arbitrage"
    return "none"

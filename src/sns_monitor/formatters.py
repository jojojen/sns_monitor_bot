from __future__ import annotations

from typing import TYPE_CHECKING

from .models import AccountWatch, KeywordWatch, Tweet, TrendWatch

if TYPE_CHECKING:
    from .signal_classifier import SnsPostSignal


def format_account_notification(rule: AccountWatch, tweets: list[Tweet]) -> str:
    """Format notification for new account tweets."""
    lines = [
        "🐦 X 帳號通知",
        f"追蹤帳號：@{rule.screen_name} ({rule.label})",
        f"發現 {len(tweets)} 則新推文：",
    ]
    if rule.include_keywords:
        lines.insert(2, f"篩選關鍵字：{', '.join(rule.include_keywords)}")

    for tweet in tweets[:5]:
        snippet = tweet.text[:100]
        lines.append(f"• {snippet}")
        lines.append(f"  {tweet.url}")

    if len(tweets) > 5:
        lines.append(f"…以及另外 {len(tweets) - 5} 則")

    return "\n".join(lines)


def format_keyword_notification(rule: KeywordWatch, tweets: list[Tweet]) -> str:
    """Format notification for keyword search results."""
    lines = [
        "🔍 X 關鍵字通知",
        f"搜尋：{rule.query} ({rule.label})",
        f"發現 {len(tweets)} 則新推文：",
    ]

    for tweet in tweets[:5]:
        snippet = tweet.text[:100]
        lines.append(f"• @{tweet.author_handle}: {snippet}")
        lines.append(f"  {tweet.url}")

    if len(tweets) > 5:
        lines.append(f"…以及另外 {len(tweets) - 5} 則")

    return "\n".join(lines)


def format_account_post_one(
    rule: AccountWatch,
    tweet: Tweet,
    *,
    feedback_counts: dict[str, int] | None = None,
) -> str:
    """One-post notification (so each post can carry its own feedback keyboard).

    ``feedback_counts`` is an optional aggregate of the rule's past-30-day
    feedback (keys: 'up' / 'down' / 'bought'). When provided, a footer
    "📊 此帳號累計：👍 N / 👎 M / 💰 K" is appended so the user has signal
    history visible while deciding whether to tap a button.
    """
    snippet = (tweet.text or "").strip()
    if len(snippet) > 240:
        snippet = snippet[:240] + "…"
    lines = [
        f"🐦 X 帳號通知 — @{rule.screen_name} ({rule.label})",
        f"時間：{tweet.created_at.strftime('%Y-%m-%d %H:%M')} UTC",
        "",
        snippet,
        "",
        tweet.url,
    ]
    if feedback_counts:
        up = feedback_counts.get("up", 0)
        down = feedback_counts.get("down", 0)
        bought = feedback_counts.get("bought", 0)
        if up or down or bought:
            lines.append(f"📊 此帳號累計：👍 {up} / 👎 {down} / 💰 {bought}（過去 30 天）")
    return "\n".join(lines)


def format_keyword_post_one(
    rule: KeywordWatch,
    tweet: Tweet,
    *,
    feedback_counts: dict[str, int] | None = None,
) -> str:
    """One-post notification for keyword-watch hits."""
    snippet = (tweet.text or "").strip()
    if len(snippet) > 240:
        snippet = snippet[:240] + "…"
    lines = [
        f"🔍 X 關鍵字通知 — {rule.query} ({rule.label})",
        f"作者：@{tweet.author_handle}",
        f"時間：{tweet.created_at.strftime('%Y-%m-%d %H:%M')} UTC",
        "",
        snippet,
        "",
        tweet.url,
    ]
    if feedback_counts:
        up = feedback_counts.get("up", 0)
        down = feedback_counts.get("down", 0)
        bought = feedback_counts.get("bought", 0)
        if up or down or bought:
            lines.append(f"📊 此規則累計：👍 {up} / 👎 {down} / 💰 {bought}（過去 30 天）")
    return "\n".join(lines)


def build_sns_feedback_keyboard(*, tweet_id: str, rule_id: str) -> dict[str, object]:
    """Inline keyboard with 👍 / 👎 / 💰 buttons for a single SNS post.

    Callback data shape: ``snsfb:<kind>:<tweet_id>:<rule_id>`` — small enough
    to stay under Telegram's 64-byte payload cap for typical IDs.
    """
    return {
        "inline_keyboard": [[
            {"text": "👍 有用", "callback_data": f"snsfb:up:{tweet_id}:{rule_id}"},
            {"text": "👎 不感興趣", "callback_data": f"snsfb:down:{tweet_id}:{rule_id}"},
            {"text": "💰 我下手了", "callback_data": f"snsfb:bought:{tweet_id}:{rule_id}"},
        ]]
    }


# ── Two-opportunity signal notifications ───────────────────────────────────


_LONG_TERM_HEADLINE = "📈 長期潛力訊號"
_ARBITRAGE_HEADLINE = "⚡ 立即套利訊號"
_COMBINED_HEADLINE = "📈⚡ 雙重訊號（長期 + 立即）"


def _truncate_tweet(text: str, limit: int = 240) -> str:
    cleaned = (text or "").strip()
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[:limit] + "…"


def _signal_subject_handle(rule: AccountWatch | KeywordWatch, tweet: Tweet) -> str:
    """Pick the right handle for the headline. Account watches name the rule's
    target; keyword watches use the tweet author since the rule itself isn't
    a single account."""
    if isinstance(rule, AccountWatch):
        return f"@{rule.screen_name}"
    return f"@{tweet.author_handle}"


def _rule_label(rule: AccountWatch | KeywordWatch) -> str:
    return rule.label or (rule.screen_name if isinstance(rule, AccountWatch) else rule.query)


def _signal_body_lines(
    *,
    signal: "SnsPostSignal",
    tweet: Tweet,
    feedback_counts: dict[str, int] | None,
) -> list[str]:
    lines: list[str] = []
    lines.append(f"時間：{tweet.created_at.strftime('%Y-%m-%d %H:%M')} UTC")

    if signal.matched_products:
        lines.append("匹配商品：" + "、".join(signal.matched_products) + "（你已 watchlist / pin）")
    if signal.matched_keywords:
        lines.append("關鍵字：" + "、".join(signal.matched_keywords))
    if signal.matched_entities:
        lines.append("提及實體：" + "、".join(signal.matched_entities))
    if signal.suggested_action:
        lines.append("建議：" + signal.suggested_action)
    if signal.deadline_iso:
        lines.append("截止：" + signal.deadline_iso)
    if signal.rationale:
        lines.append("理由：" + signal.rationale)

    lines.append("")
    lines.append(_truncate_tweet(tweet.text))
    lines.append("")
    lines.append(tweet.url)

    if feedback_counts:
        up = feedback_counts.get("up", 0)
        down = feedback_counts.get("down", 0)
        bought = feedback_counts.get("bought", 0)
        if up or down or bought:
            lines.append(
                f"📊 此規則累計：👍 {up} / 👎 {down} / 💰 {bought}（過去 30 天）"
            )
    return lines


def _signal_notification(
    *,
    headline: str,
    rule: AccountWatch | KeywordWatch,
    tweet: Tweet,
    signal: "SnsPostSignal",
    bypass_keyword: str | None = None,
    feedback_counts: dict[str, int] | None = None,
) -> str:
    """Compose a complete signal notification.

    When ``bypass_keyword`` is non-empty, prepends a one-line header explaining
    that the post was let through because the user's own filter keyword matched
    (Bypass A) — so the user can tell signal-driven pushes from keyword pushes.
    """
    lines: list[str] = []
    if bypass_keyword:
        lines.append(f"✅ 命中你設的篩選關鍵字「{bypass_keyword}」（一律通知）")
    handle = _signal_subject_handle(rule, tweet)
    label = _rule_label(rule)
    lines.append(f"{headline} — {handle}（{label}）")
    lines.extend(_signal_body_lines(signal=signal, tweet=tweet, feedback_counts=feedback_counts))
    return "\n".join(lines)


def format_long_term_signal_notification(
    *,
    rule: AccountWatch | KeywordWatch,
    tweet: Tweet,
    signal: "SnsPostSignal",
    bypass_keyword: str | None = None,
    feedback_counts: dict[str, int] | None = None,
) -> str:
    """📈 long-term-only signal notification."""
    return _signal_notification(
        headline=_LONG_TERM_HEADLINE,
        rule=rule, tweet=tweet, signal=signal,
        bypass_keyword=bypass_keyword, feedback_counts=feedback_counts,
    )


def format_arbitrage_signal_notification(
    *,
    rule: AccountWatch | KeywordWatch,
    tweet: Tweet,
    signal: "SnsPostSignal",
    bypass_keyword: str | None = None,
    feedback_counts: dict[str, int] | None = None,
) -> str:
    """⚡ immediate-arbitrage-only signal notification."""
    return _signal_notification(
        headline=_ARBITRAGE_HEADLINE,
        rule=rule, tweet=tweet, signal=signal,
        bypass_keyword=bypass_keyword, feedback_counts=feedback_counts,
    )


def format_combined_signal_notification(
    *,
    rule: AccountWatch | KeywordWatch,
    tweet: Tweet,
    signal: "SnsPostSignal",
    bypass_keyword: str | None = None,
    feedback_counts: dict[str, int] | None = None,
) -> str:
    """📈⚡ both-signal notification (long_term and arbitrage both fired)."""
    return _signal_notification(
        headline=_COMBINED_HEADLINE,
        rule=rule, tweet=tweet, signal=signal,
        bypass_keyword=bypass_keyword, feedback_counts=feedback_counts,
    )


def format_signal_notification(
    *,
    rule: AccountWatch | KeywordWatch,
    tweet: Tweet,
    signal: "SnsPostSignal",
    bypass_reason: str,
    bypass_keyword: str | None = None,
    feedback_counts: dict[str, int] | None = None,
) -> str:
    """Pick the right signal formatter based on ``bypass_reason``.

    bypass_reason values (from ``signal_classifier.decide_push_reason``):
      'explicit_keyword' / 'both' / 'long_term' / 'arbitrage'
    For 'explicit_keyword' (Bypass A), the headline is chosen by whichever
    score is higher; if both are zero we still show as long_term — the
    bypass header above already explains why this is here.
    """
    if bypass_reason == "both":
        headline = _COMBINED_HEADLINE
    elif bypass_reason == "arbitrage":
        headline = _ARBITRAGE_HEADLINE
    elif bypass_reason == "long_term":
        headline = _LONG_TERM_HEADLINE
    else:  # explicit_keyword
        if signal.arbitrage_score > signal.long_term_score:
            headline = _ARBITRAGE_HEADLINE
        else:
            headline = _LONG_TERM_HEADLINE
    return _signal_notification(
        headline=headline,
        rule=rule, tweet=tweet, signal=signal,
        bypass_keyword=bypass_keyword, feedback_counts=feedback_counts,
    )


def format_trend_notification(rule: TrendWatch, new_trends: list[str], all_trends: list[str]) -> str:
    """Format notification for trend updates."""
    lines = [
        "🔥 X 熱門話題通知",
        f"分類：{rule.category} ({rule.label})",
        f"新增 {len(new_trends)} 項熱門話題：",
    ]

    for trend in new_trends[:10]:
        lines.append(f"• {trend}")

    if len(new_trends) > 10:
        lines.append(f"…及另外 {len(new_trends) - 10} 項")

    return "\n".join(lines)

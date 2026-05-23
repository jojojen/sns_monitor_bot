from __future__ import annotations

from .models import AccountWatch, KeywordWatch, Tweet, TrendWatch


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

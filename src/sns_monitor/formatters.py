from __future__ import annotations

from .models import AccountWatch, KeywordWatch, Tweet, TrendWatch


def format_account_notification(rule: AccountWatch, tweets: list[Tweet]) -> str:
    """Format notification for new account tweets."""
    lines = [
        "🐦 X 帳號通知",
        f"追蹤帳號：@{rule.screen_name} ({rule.label})",
        f"發現 {len(tweets)} 則新推文：",
    ]

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

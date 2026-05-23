from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal, Union

WatchKind = Literal["account", "keyword", "trend"]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class Tweet:
    tweet_id: str
    author_handle: str
    author_id: str
    text: str
    created_at: datetime
    lang: str | None = None
    retweet_count: int = 0
    like_count: int = 0
    url: str = ""


# Soft enum of domain tags. Watch rules carry a `domains` tuple; topic-specific
# agents (TCG opportunity, future stock/news agents, …) filter to rules whose
# domains intersect their own domain set. Free-text values are accepted (we
# only normalise case at parse time) but the LLM is prompted to prefer these.
RECOMMENDED_DOMAINS: tuple[str, ...] = (
    "pokemon",
    "yugioh",
    "ws",
    "union_arena",
    "tcg",
    "politic",
    "stock",
    "news",
    "gaming",
    "entertainment",
    "anime",
    "gundam",
    "other",
)

TCG_DOMAINS: frozenset[str] = frozenset({"pokemon", "yugioh", "ws", "union_arena", "tcg"})


def normalize_domain(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = value.strip().lower().replace(" ", "_").replace("-", "_")
    return cleaned or None


def normalize_domains(values: object) -> tuple[str, ...]:
    if not isinstance(values, (list, tuple)):
        return ()
    cleaned: list[str] = []
    seen: set[str] = set()
    for raw in values:
        normalised = normalize_domain(raw)
        if normalised and normalised not in seen:
            seen.add(normalised)
            cleaned.append(normalised)
    return tuple(cleaned)


@dataclass(frozen=True)
class AccountWatch:
    rule_id: str
    screen_name: str
    user_id: str | None
    label: str
    include_keywords: tuple[str, ...] = ()
    domains: tuple[str, ...] = ()
    enabled: bool = True
    schedule_minutes: int = 15
    chat_id: str = ""
    last_checked_at: datetime | None = None
    source: str = "x"
    cooldown_until: str | None = None


@dataclass(frozen=True)
class KeywordWatch:
    rule_id: str
    query: str
    label: str
    domains: tuple[str, ...] = ()
    enabled: bool = True
    schedule_minutes: int = 30
    chat_id: str = ""
    last_checked_at: datetime | None = None
    source: str = "x"
    cooldown_until: str | None = None


@dataclass(frozen=True)
class TrendWatch:
    rule_id: str
    category: str
    label: str
    domains: tuple[str, ...] = ()
    enabled: bool = True
    schedule_minutes: int = 60
    chat_id: str = ""
    last_checked_at: datetime | None = None
    source: str = "x"
    cooldown_until: str | None = None


WatchRule = Union[AccountWatch, KeywordWatch, TrendWatch]


@dataclass(frozen=True)
class TrendSnapshot:
    snapshot_id: str
    rule_id: str
    names: tuple[str, ...]
    captured_at: datetime = field(default_factory=utc_now)

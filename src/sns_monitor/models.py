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


@dataclass(frozen=True)
class AccountWatch:
    rule_id: str
    screen_name: str
    user_id: str | None
    label: str
    include_keywords: tuple[str, ...] = ()
    enabled: bool = True
    schedule_minutes: int = 15
    chat_id: str = ""
    last_checked_at: datetime | None = None


@dataclass(frozen=True)
class KeywordWatch:
    rule_id: str
    query: str
    label: str
    enabled: bool = True
    schedule_minutes: int = 30
    chat_id: str = ""
    last_checked_at: datetime | None = None


@dataclass(frozen=True)
class TrendWatch:
    rule_id: str
    category: str
    label: str
    enabled: bool = True
    schedule_minutes: int = 60
    chat_id: str = ""
    last_checked_at: datetime | None = None


WatchRule = Union[AccountWatch, KeywordWatch, TrendWatch]


@dataclass(frozen=True)
class TrendSnapshot:
    snapshot_id: str
    rule_id: str
    names: tuple[str, ...]
    captured_at: datetime = field(default_factory=utc_now)

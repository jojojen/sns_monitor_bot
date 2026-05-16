from __future__ import annotations

import sys
from datetime import datetime, timezone

import pytest

from sns_monitor.filters import (
    filter_tweets_by_keywords,
    normalize_keyword_filters,
    parse_account_watch_text,
    parse_keyword_filter_text,
    tweet_matches_keyword_filters,
)
from sns_monitor.models import AccountWatch, Tweet
from sns_monitor.monitor import SnsMonitor
from sns_monitor.storage import SnsDatabase


def _tweet(tweet_id: str, text: str, *, author: str = "elonmusk") -> Tweet:
    return Tweet(
        tweet_id=tweet_id,
        author_handle=author,
        author_id="123",
        text=text,
        created_at=datetime(2026, 5, 15, tzinfo=timezone.utc),
        url=f"https://x.com/{author}/status/{tweet_id}",
    )


class _FakeXClient:
    def __init__(self, tweets: list[Tweet]) -> None:
        self.tweets = tweets

    async def resolve_user_id(self, screen_name: str) -> str:
        return screen_name.lstrip("@")

    async def get_timeline(self, user_id: str) -> list[Tweet]:
        return self.tweets


def test_normalize_keyword_filters_accepts_json_array() -> None:
    assert normalize_keyword_filters('["buy", "sell"]') == ("buy", "sell")


def test_normalize_keyword_filters_accepts_split_json_array_tokens() -> None:
    assert normalize_keyword_filters(['["buy",', '"sell"]']) == ("buy", "sell")


def test_normalize_keyword_filters_accepts_comma_separated_text() -> None:
    assert normalize_keyword_filters("buy, sell,hold") == ("buy", "sell", "hold")


def test_normalize_keyword_filters_dedupes_case_insensitively() -> None:
    assert normalize_keyword_filters(["Buy", "buy", " SELL "]) == ("Buy", "SELL")


def test_parse_keyword_filter_text_supports_shell_style_quotes() -> None:
    assert parse_keyword_filter_text('"strong buy" sell') == ("strong buy", "sell")


def test_parse_account_watch_text_extracts_handle_and_filters() -> None:
    # Legacy JSON-array filter form continues to work; no `domain[...]` so
    # the third tuple entry is None (caller preserves existing rule domain).
    assert parse_account_watch_text('@elonmusk ["buy", "sell"]') == (
        "elonmusk",
        ("buy", "sell"),
        None,
    )


def test_parse_account_watch_text_accepts_full_width_filter_brackets() -> None:
    assert parse_account_watch_text("@tenbai_hakase ［抽選］ 篩選") == (
        "tenbai_hakase",
        ("抽選",),
        None,
    )


def test_parse_account_watch_text_includes_real_donald_trump_buy_sell_filter() -> None:
    assert parse_account_watch_text('@realDonaldTrump ["buy", "sell"]') == (
        "realDonaldTrump",
        ("buy", "sell"),
        None,
    )


def test_parse_account_watch_text_extracts_labeled_filter_and_domain_brackets() -> None:
    # New syntax: explicit filter[...] and domain[...] labels.
    assert parse_account_watch_text("@Laurier_News filter[抽選] domain[pokemon, yugioh]") == (
        "Laurier_News",
        ("抽選",),
        ("pokemon", "yugioh"),
    )


def test_parse_account_watch_text_domain_only_preserves_legacy_filter() -> None:
    # Domain provided but no filter[...] → caller can preserve existing
    # filter (we signal that by returning empty tuple for filter and the
    # parsed domain tuple).
    assert parse_account_watch_text("@elonmusk domain[stock, politic]") == (
        "elonmusk",
        (),
        ("stock", "politic"),
    )


def test_tweet_without_filters_always_matches() -> None:
    assert tweet_matches_keyword_filters(_tweet("1", "anything at all"), ())


def test_tweet_matches_keyword_filters_case_insensitively() -> None:
    assert tweet_matches_keyword_filters(_tweet("1", "Analysts say this is a BUY signal"), ("buy",))


def test_tweet_rejects_when_no_keyword_is_present() -> None:
    assert not tweet_matches_keyword_filters(_tweet("1", "holding steady today"), ("buy", "sell"))


def test_filter_tweets_by_keywords_keeps_only_matching_tweets() -> None:
    tweets = [_tweet("1", "buy now"), _tweet("2", "just vibes"), _tweet("3", "sell later")]
    assert [tweet.tweet_id for tweet in filter_tweets_by_keywords(tweets, ("buy", "sell"))] == ["1", "3"]


def test_storage_round_trips_account_keyword_filters(tmp_path) -> None:
    db = SnsDatabase(tmp_path / "sns.sqlite3")
    db.bootstrap()
    rule_id = SnsDatabase._watch_rule_id("account", "elonmusk")
    db.save_watch_rule(
        AccountWatch(
            rule_id=rule_id,
            screen_name="elonmusk",
            user_id=None,
            label="@elonmusk",
            include_keywords=("buy", "sell"),
            chat_id="123",
        )
    )

    rule = db.get_watch_rule(rule_id)

    assert isinstance(rule, AccountWatch)
    assert rule.include_keywords == ("buy", "sell")


def test_storage_reads_legacy_account_rules_without_filters(tmp_path) -> None:
    db = SnsDatabase(tmp_path / "sns.sqlite3")
    db.bootstrap()
    now = datetime(2026, 5, 15, tzinfo=timezone.utc).isoformat()
    rule_id = SnsDatabase._watch_rule_id("account", "aka_claw")
    with db.connect() as conn:
        conn.execute(
            """
            INSERT INTO watch_rules
            (rule_id, kind, label, query_json, enabled, schedule_minutes, chat_id, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                rule_id,
                "account",
                "@aka_claw",
                '{"screen_name": "aka_claw", "user_id": null}',
                1,
                15,
                "123",
                now,
                now,
            ),
        )
        conn.commit()

    rule = db.get_watch_rule(rule_id)

    assert isinstance(rule, AccountWatch)
    assert rule.include_keywords == ()


def test_save_watch_rule_preserves_last_checked_when_updating_filters(tmp_path) -> None:
    db = SnsDatabase(tmp_path / "sns.sqlite3")
    db.bootstrap()
    rule_id = SnsDatabase._watch_rule_id("account", "elonmusk")
    db.save_watch_rule(
        AccountWatch(rule_id=rule_id, screen_name="elonmusk", user_id=None, label="@elonmusk", chat_id="123")
    )
    db.mark_rule_checked(rule_id)
    checked_at = db.get_watch_rule(rule_id).last_checked_at

    db.save_watch_rule(
        AccountWatch(
            rule_id=rule_id,
            screen_name="elonmusk",
            user_id=None,
            label="@elonmusk",
            include_keywords=("buy", "sell"),
            chat_id="123",
        )
    )

    assert db.get_watch_rule(rule_id).last_checked_at == checked_at


@pytest.mark.asyncio
async def test_account_monitor_notifies_only_matching_new_tweets(tmp_path) -> None:
    db_path = tmp_path / "sns.sqlite3"
    db = SnsDatabase(db_path)
    db.bootstrap()
    rule_id = SnsDatabase._watch_rule_id("account", "elonmusk")
    db.save_watch_rule(
        AccountWatch(
            rule_id=rule_id,
            screen_name="elonmusk",
            user_id="elonmusk",
            label="@elonmusk",
            include_keywords=("buy", "sell"),
            chat_id="123",
        )
    )
    db.mark_rule_checked(rule_id)

    notifications: list[str] = []
    monitor = SnsMonitor(
        db_path=db_path,
        x_client=_FakeXClient([_tweet("1", "time to buy"), _tweet("2", "unrelated note")]),
        notify_fn=lambda _chat_id, text: notifications.append(text),
    )

    await monitor._check_account_watch(db.get_watch_rule(rule_id))

    assert len(notifications) == 1
    assert "time to buy" in notifications[0]
    assert "unrelated note" not in notifications[0]


@pytest.mark.asyncio
async def test_account_monitor_records_but_does_not_notify_non_matching_tweets(tmp_path) -> None:
    db_path = tmp_path / "sns.sqlite3"
    db = SnsDatabase(db_path)
    db.bootstrap()
    rule_id = SnsDatabase._watch_rule_id("account", "elonmusk")
    db.save_watch_rule(
        AccountWatch(
            rule_id=rule_id,
            screen_name="elonmusk",
            user_id="elonmusk",
            label="@elonmusk",
            include_keywords=("buy", "sell"),
            chat_id="123",
        )
    )
    db.mark_rule_checked(rule_id)

    notifications: list[str] = []
    monitor = SnsMonitor(
        db_path=db_path,
        x_client=_FakeXClient([_tweet("10", "holding steady")]),
        notify_fn=lambda _chat_id, text: notifications.append(text),
    )

    await monitor._check_account_watch(db.get_watch_rule(rule_id))

    assert notifications == []
    with db.connect() as conn:
        row = conn.execute(
            "SELECT notified FROM seen_tweets WHERE rule_id = ? AND tweet_id = ?",
            (rule_id, "10"),
        ).fetchone()
    assert row["notified"] == 0


@pytest.mark.asyncio
async def test_account_monitor_first_scan_still_baselines_filtered_rules(tmp_path) -> None:
    db_path = tmp_path / "sns.sqlite3"
    db = SnsDatabase(db_path)
    db.bootstrap()
    rule_id = SnsDatabase._watch_rule_id("account", "elonmusk")
    db.save_watch_rule(
        AccountWatch(
            rule_id=rule_id,
            screen_name="elonmusk",
            user_id="elonmusk",
            label="@elonmusk",
            include_keywords=("buy", "sell"),
            chat_id="123",
        )
    )

    notifications: list[str] = []
    monitor = SnsMonitor(
        db_path=db_path,
        x_client=_FakeXClient([_tweet("20", "buy now"), _tweet("21", "plain update")]),
        notify_fn=lambda _chat_id, text: notifications.append(text),
    )

    await monitor._check_account_watch(db.get_watch_rule(rule_id))

    assert notifications == []
    with db.connect() as conn:
        count = conn.execute("SELECT COUNT(*) AS c FROM seen_tweets WHERE rule_id = ?", (rule_id,)).fetchone()["c"]
    assert count == 2


def test_cli_add_account_stores_keyword_filters(tmp_path, monkeypatch) -> None:
    from sns_monitor.__main__ import main

    db_path = tmp_path / "sns.sqlite3"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "sns-monitor",
            "add-account",
            "@elonmusk",
            "--chat-id",
            "123",
            "--keywords",
            "buy",
            "sell",
            "--db",
            str(db_path),
        ],
    )

    assert main() == 0
    db = SnsDatabase(db_path)
    rule = db.get_watch_rule(SnsDatabase._watch_rule_id("account", "elonmusk"))

    assert isinstance(rule, AccountWatch)
    assert rule.include_keywords == ("buy", "sell")

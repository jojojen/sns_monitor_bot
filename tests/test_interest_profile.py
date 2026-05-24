"""Unit tests for UserInterestProfile cross-DB aggregation."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from sns_monitor.interest_profile import (
    aggregate_rule_feedback,
    build_user_interest_profile,
)


def _make_marketplace_watchlist_db(path: Path, rows: list[tuple[str, str, int]]) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE marketplace_watchlist (
            id INTEGER PRIMARY KEY,
            chat_id TEXT NOT NULL,
            query TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    for chat_id, query, enabled in rows:
        conn.execute(
            "INSERT INTO marketplace_watchlist (chat_id, query, enabled) VALUES (?, ?, ?)",
            (chat_id, query, enabled),
        )
    conn.commit()
    conn.close()


def _make_opportunity_candidates_db(path: Path, rows: list[tuple[str, int, str]]) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE opportunity_candidates (
            id INTEGER PRIMARY KEY,
            title TEXT NOT NULL,
            is_target INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'active',
            updated_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    for title, is_target, status in rows:
        conn.execute(
            "INSERT INTO opportunity_candidates (title, is_target, status) VALUES (?, ?, ?)",
            (title, is_target, status),
        )
    conn.commit()
    conn.close()


def _make_sns_feedback_db(path: Path, rows: list[tuple[str, str, str, str]]) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE sns_post_feedback (
            chat_id TEXT NOT NULL,
            rule_id TEXT NOT NULL,
            tweet_id TEXT NOT NULL,
            feedback_kind TEXT NOT NULL,
            feedback_at TEXT NOT NULL
        )
        """
    )
    for chat_id, rule_id, kind, when in rows:
        conn.execute(
            "INSERT INTO sns_post_feedback (chat_id, rule_id, tweet_id, feedback_kind, feedback_at)"
            " VALUES (?, ?, 't', ?, ?)",
            (chat_id, rule_id, kind, when),
        )
    conn.commit()
    conn.close()


def test_profile_aggregates_all_three_sources(tmp_path):
    monitor = tmp_path / "monitor.sqlite3"
    opportunity = tmp_path / "opp.sqlite3"
    sns = tmp_path / "sns.sqlite3"
    _make_marketplace_watchlist_db(monitor, [
        ("123", "アビスアイ box", 1),
        ("123", "水野愛 ssp", 1),
        ("123", "disabled item", 0),
        ("999", "other chat", 1),
    ])
    _make_opportunity_candidates_db(opportunity, [
        ("ピカチュウex SAR", 1, "active"),
        ("リーフィアex SAR", 0, "active"),
        ("Closed item", 1, "closed"),
    ])
    _make_sns_feedback_db(sns, [
        ("123", "rule-a", "up", "2026-05-23T10:00:00+00:00"),
        ("123", "rule-a", "up", "2026-05-23T11:00:00+00:00"),
        ("123", "rule-a", "bought", "2026-05-23T12:00:00+00:00"),
        ("123", "rule-b", "down", "2026-05-23T13:00:00+00:00"),
        ("999", "rule-a", "up", "2026-05-23T14:00:00+00:00"),  # different chat
    ])

    profile = build_user_interest_profile(
        chat_id="123",
        sns_db_path=sns,
        monitor_db_path=monitor,
        opportunity_db_path=opportunity,
    )

    assert "アビスアイ box" in profile.watchlist_queries
    assert "水野愛 ssp" in profile.watchlist_queries
    assert "disabled item" not in profile.watchlist_queries
    assert "other chat" not in profile.watchlist_queries

    assert profile.pinned_targets == ("ピカチュウex SAR",)

    rule_a = aggregate_rule_feedback(profile, "rule-a")
    assert rule_a["up"] == 2
    assert rule_a["bought"] == 1
    assert rule_a["down"] == 0
    rule_b = aggregate_rule_feedback(profile, "rule-b")
    assert rule_b["down"] == 1

    assert not profile.is_empty()


def test_profile_handles_missing_dbs_gracefully(tmp_path):
    """All three DBs missing → empty profile, no exception."""
    profile = build_user_interest_profile(
        chat_id="123",
        sns_db_path=tmp_path / "absent_sns.sqlite3",
        monitor_db_path=tmp_path / "absent_monitor.sqlite3",
        opportunity_db_path=tmp_path / "absent_opportunity.sqlite3",
    )
    assert profile.watchlist_queries == ()
    assert profile.pinned_targets == ()
    assert profile.feedback_by_rule == {}
    assert profile.is_empty()


def test_profile_handles_existing_db_with_missing_table(tmp_path):
    """DB file exists but lacks the expected table — should not raise."""
    monitor = tmp_path / "monitor.sqlite3"
    sqlite3.connect(monitor).close()  # empty DB, no marketplace_watchlist

    profile = build_user_interest_profile(
        chat_id="1",
        sns_db_path=tmp_path / "absent.sqlite3",
        monitor_db_path=monitor,
        opportunity_db_path=tmp_path / "absent.sqlite3",
    )
    assert profile.watchlist_queries == ()


def test_aggregate_rule_feedback_returns_zeros_for_unknown_rule():
    from sns_monitor.interest_profile import UserInterestProfile
    profile = UserInterestProfile(chat_id="x", feedback_by_rule={"rule-a": {"up": 5}})
    assert aggregate_rule_feedback(profile, "rule-b") == {"up": 0, "down": 0, "bought": 0}

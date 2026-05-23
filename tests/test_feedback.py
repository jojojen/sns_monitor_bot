"""Tests for the SNS post feedback loop (storage + record_sns_feedback)."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from sns_monitor.feedback import (
    DEFAULT_DOWN_DISMISS_THRESHOLD,
    record_sns_feedback,
)
from sns_monitor.models import AccountWatch
from sns_monitor.storage import SnsDatabase


def _account_watch(
    *, rule_id: str = "rule_a", schedule_minutes: int = 60,
) -> AccountWatch:
    return AccountWatch(
        rule_id=rule_id,
        screen_name="testuser",
        user_id=None,
        label="testuser",
        include_keywords=(),
        domains=(),
        enabled=True,
        schedule_minutes=schedule_minutes,
        chat_id="123",
    )


def _make_db(tmp_path: Path) -> SnsDatabase:
    db = SnsDatabase(tmp_path / "sns.sqlite3")
    db.bootstrap()
    return db


def _row(db: SnsDatabase, sql: str, params: tuple = ()) -> dict | None:
    with sqlite3.connect(db.path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(sql, params).fetchone()
    return dict(row) if row else None


# ── Schema migration ─────────────────────────────────────────────────────────


def test_bootstrap_creates_feedback_table_and_cooldown_column(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    with sqlite3.connect(db.path) as conn:
        tbls = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        cols = {r[1] for r in conn.execute("PRAGMA table_info(watch_rules)")}
    assert "sns_post_feedback" in tbls
    assert "cooldown_until" in cols


# ── 👍 up: record only ──────────────────────────────────────────────────────


def test_up_feedback_records_only(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    rule = _account_watch(schedule_minutes=60)
    db.save_watch_rule(rule)

    result = record_sns_feedback(
        db=db, tweet_id="t1", rule_id=rule.rule_id, chat_id="123", kind="up",
    )
    assert result["status"] == "ok"
    assert result["side_effects"] == []
    # Feedback row written
    row = _row(db, "SELECT * FROM sns_post_feedback WHERE tweet_id = ?", ("t1",))
    assert row is not None and row["feedback_kind"] == "up"
    # Rule unchanged
    refreshed = db.get_watch_rule(rule.rule_id)
    assert refreshed.schedule_minutes == 60
    assert refreshed.cooldown_until is None
    assert refreshed.enabled is True


# ── 💰 bought: halve schedule (floor 15) ────────────────────────────────────


def test_bought_feedback_halves_schedule(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    db.save_watch_rule(_account_watch(schedule_minutes=120))

    result = record_sns_feedback(
        db=db, tweet_id="t1", rule_id="rule_a", chat_id="123", kind="bought",
    )
    assert "rule_schedule_shortened" in result["side_effects"]
    assert result["new_schedule_minutes"] == 60
    assert db.get_watch_rule("rule_a").schedule_minutes == 60


def test_bought_feedback_floors_at_15(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    db.save_watch_rule(_account_watch(schedule_minutes=20))

    record_sns_feedback(
        db=db, tweet_id="t1", rule_id="rule_a", chat_id="123", kind="bought",
    )
    # 20 // 2 = 10 → clamped to 15
    assert db.get_watch_rule("rule_a").schedule_minutes == 15


def test_bought_feedback_at_floor_no_side_effect(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    db.save_watch_rule(_account_watch(schedule_minutes=15))

    result = record_sns_feedback(
        db=db, tweet_id="t1", rule_id="rule_a", chat_id="123", kind="bought",
    )
    # Schedule already at 15 → no shortening
    assert "rule_schedule_shortened" not in result["side_effects"]
    assert db.get_watch_rule("rule_a").schedule_minutes == 15


# ── 👎 down: cooldown → auto-disable ─────────────────────────────────────────


def test_down_feedback_starts_24h_cooldown(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    db.save_watch_rule(_account_watch())

    result = record_sns_feedback(
        db=db, tweet_id="t1", rule_id="rule_a", chat_id="123", kind="down",
    )
    assert "cooldown_started" in result["side_effects"]
    refreshed = db.get_watch_rule("rule_a")
    assert refreshed.cooldown_until is not None
    until = datetime.fromisoformat(refreshed.cooldown_until)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    assert timedelta(hours=23) < (until - now) < timedelta(hours=25)
    assert refreshed.enabled is True  # not disabled on first down


def test_three_downs_auto_disable_rule(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    db.save_watch_rule(_account_watch())
    for i in range(DEFAULT_DOWN_DISMISS_THRESHOLD - 1):
        result = record_sns_feedback(
            db=db, tweet_id=f"t{i}", rule_id="rule_a", chat_id="123", kind="down",
        )
        assert "rule_disabled" not in result["side_effects"]
        assert db.get_watch_rule("rule_a").enabled is True
    final = record_sns_feedback(
        db=db,
        tweet_id=f"t{DEFAULT_DOWN_DISMISS_THRESHOLD}",
        rule_id="rule_a", chat_id="123", kind="down",
    )
    assert "rule_disabled" in final["side_effects"]
    assert db.get_watch_rule("rule_a").enabled is False
    # cooldown is cleared once the rule is disabled (no longer relevant)
    assert db.get_watch_rule("rule_a").cooldown_until is None


# ── Unknown kind / missing rule ──────────────────────────────────────────────


def test_unknown_kind_rejected(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    db.save_watch_rule(_account_watch())
    result = record_sns_feedback(
        db=db, tweet_id="t1", rule_id="rule_a", chat_id="123", kind="meh",
    )
    assert result["status"] == "rejected"


def test_missing_rule_rejected(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    result = record_sns_feedback(
        db=db, tweet_id="t1", rule_id="rule_doesnt_exist",
        chat_id="123", kind="up",
    )
    assert result["status"] == "rejected"


# ── Aggregation ──────────────────────────────────────────────────────────────


def test_feedback_counts_for_rule_aggregates(tmp_path: Path) -> None:
    db = _make_db(tmp_path)
    db.save_watch_rule(_account_watch())
    record_sns_feedback(db=db, tweet_id="t1", rule_id="rule_a", chat_id="123", kind="up")
    record_sns_feedback(db=db, tweet_id="t2", rule_id="rule_a", chat_id="123", kind="up")
    # Use a fresh rule for bought (to avoid cooldown from previous tests)
    record_sns_feedback(db=db, tweet_id="t3", rule_id="rule_a", chat_id="123", kind="bought")
    since = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    counts = db.feedback_counts_for_rule(rule_id="rule_a", since_iso=since)
    assert counts.get("up") == 2
    assert counts.get("bought") == 1

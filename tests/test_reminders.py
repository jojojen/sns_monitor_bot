"""scheduled_reminders storage + monitor due-check tests.

Covers:
- create_reminder / list_due_reminders / mark_reminder_sent
- dedup on (tweet_id, rule_id, kind); signup + event coexist
- list_due_reminders only returns unsent rows with remind_at <= now
- _check_due_reminders fires notify_fn once per due row and marks it sent
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path

from sns_monitor.models import utc_now
from sns_monitor.storage import SnsDatabase


def _db(tmp_path: Path) -> SnsDatabase:
    db = SnsDatabase(tmp_path / "rem.db")
    db.bootstrap()
    return db


def test_bootstrap_creates_scheduled_reminders_table(tmp_path: Path) -> None:
    import sqlite3

    db = _db(tmp_path)
    with sqlite3.connect(db.path) as conn:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(scheduled_reminders)")}
    assert {"reminder_id", "kind", "target_date", "remind_at", "payload_text", "sent"} <= cols


def test_create_and_list_due_reminder(tmp_path: Path) -> None:
    db = _db(tmp_path)
    past = (utc_now() - timedelta(minutes=5)).isoformat()
    inserted = db.create_reminder(
        chat_id="c1", rule_id="r1", tweet_id="t1", kind="event",
        target_date=(utc_now() + timedelta(days=1)).isoformat(),
        remind_at=past, payload_text="body",
    )
    assert inserted is True
    due = db.list_due_reminders(utc_now().isoformat())
    assert len(due) == 1
    assert due[0]["reminder_id"] == "t1:r1:event"
    assert due[0]["payload_text"] == "body"


def test_future_reminder_not_due(tmp_path: Path) -> None:
    db = _db(tmp_path)
    future = (utc_now() + timedelta(hours=10)).isoformat()
    db.create_reminder(
        chat_id="c1", rule_id="r1", tweet_id="t1", kind="event",
        target_date=future, remind_at=future, payload_text="b",
    )
    assert db.list_due_reminders(utc_now().isoformat()) == []


def test_create_reminder_dedups_on_id(tmp_path: Path) -> None:
    db = _db(tmp_path)
    now = utc_now().isoformat()
    assert db.create_reminder(
        chat_id="c1", rule_id="r1", tweet_id="t1", kind="event",
        target_date=now, remind_at=now, payload_text="first",
    ) is True
    # Same (tweet, rule, kind) → ignored, original payload preserved.
    assert db.create_reminder(
        chat_id="c1", rule_id="r1", tweet_id="t1", kind="event",
        target_date=now, remind_at=now, payload_text="second",
    ) is False
    due = db.list_due_reminders(utc_now().isoformat())
    assert len(due) == 1
    assert due[0]["payload_text"] == "first"


def test_signup_and_event_reminders_coexist(tmp_path: Path) -> None:
    db = _db(tmp_path)
    now = utc_now().isoformat()
    db.create_reminder(chat_id="c", rule_id="r", tweet_id="t", kind="signup",
                       target_date=now, remind_at=now, payload_text="signup")
    db.create_reminder(chat_id="c", rule_id="r", tweet_id="t", kind="event",
                       target_date=now, remind_at=now, payload_text="event")
    due = db.list_due_reminders(utc_now().isoformat())
    kinds = {r["kind"] for r in due}
    assert kinds == {"signup", "event"}


def test_mark_reminder_sent_excludes_from_due(tmp_path: Path) -> None:
    db = _db(tmp_path)
    now = utc_now().isoformat()
    db.create_reminder(chat_id="c", rule_id="r", tweet_id="t", kind="event",
                       target_date=now, remind_at=now, payload_text="b")
    db.mark_reminder_sent("t:r:event")
    assert db.list_due_reminders(utc_now().isoformat()) == []


def test_check_due_reminders_fires_notify_and_marks_sent(tmp_path: Path) -> None:
    from sns_monitor.monitor import SnsMonitor

    sent: list[tuple[str, str]] = []

    def notify(chat_id, text, *args):
        sent.append((chat_id, text))

    db = _db(tmp_path)
    past = (utc_now() - timedelta(minutes=1)).isoformat()
    db.create_reminder(chat_id="chatX", rule_id="r", tweet_id="t", kind="event",
                       target_date=(utc_now() + timedelta(days=1)).isoformat(),
                       remind_at=past, payload_text="⏰ 活動前提醒\n\nbody")

    monitor = SnsMonitor(db_path=str(db.path), notify_fn=notify)
    asyncio.run(monitor._check_due_reminders())

    assert sent == [("chatX", "⏰ 活動前提醒\n\nbody")]
    assert db.list_due_reminders(utc_now().isoformat()) == []

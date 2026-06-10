"""Inbox for Telegram → sns_monitor write requests.

Telegram process INSERTs pending rows; sns_monitor service polls and applies them
to data/sns.sqlite3 then marks done. Single-writer-per-file: telegram never
writes sns.sqlite3 directly.

Schema is created by the *producer* (telegram) at startup so it exists before the
service even runs. The service never creates this file — it only reads/updates rows.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS sns_requests (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id      TEXT NOT NULL,
    action       TEXT NOT NULL,
    payload      TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending',
    created_at   TEXT NOT NULL,
    processed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_sns_requests_status ON sns_requests (status);
"""


class SnsInbox:
    """Thread-safe inbox for SNS write operations from the Telegram process.

    *action* values understood by the service:
    - ``save_rule``   — payload = serialized WatchRule dict (see push_rule)
    - ``delete_rule`` — payload = ``{"rule_id": "..."}``
    - ``feedback``    — payload = ``{"tweet_id": ..., "rule_id": ..., "kind": ..., "chat_id": ...}``
    - ``auto_discovery_feedback`` — payload = ``{"screen_name": ..., "polarity": ..., "domains": [...], "chat_id": ...}``
    """

    def __init__(self, db_path: str | Path) -> None:
        self.path = Path(db_path)
        self._lock = threading.Lock()

    def bootstrap(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(_CREATE_SQL)

    def push(self, action: str, payload: dict, chat_id: str = "") -> int:
        with self._lock, self._connect() as conn:
            cursor = conn.execute(
                "INSERT INTO sns_requests (chat_id, action, payload, created_at) VALUES (?,?,?,?)",
                (chat_id, action, json.dumps(payload), _utc_now()),
            )
            conn.commit()
            return cursor.lastrowid

    def push_rule(self, rule, chat_id: str = "") -> int:
        """Serialize a WatchRule and push a save_rule action."""
        from .models import AccountWatch, KeywordWatch, TrendWatch

        if isinstance(rule, AccountWatch):
            kind = "account"
            data = {
                "kind": kind,
                "rule_id": rule.rule_id,
                "label": rule.label,
                "screen_name": rule.screen_name,
                "user_id": rule.user_id,
                "include_keywords": list(rule.include_keywords),
                "domains": list(rule.domains),
                "enabled": rule.enabled,
                "schedule_minutes": rule.schedule_minutes,
                "chat_id": rule.chat_id or chat_id,
                "source": getattr(rule, "source", "x"),
                "is_auto_discovered": getattr(rule, "is_auto_discovered", False),
                "last_checked_at": (
                    rule.last_checked_at.isoformat()
                    if rule.last_checked_at else None
                ),
            }
        elif isinstance(rule, KeywordWatch):
            kind = "keyword"
            data = {
                "kind": kind,
                "rule_id": rule.rule_id,
                "label": rule.label,
                "query": rule.query,
                "domains": list(rule.domains),
                "enabled": rule.enabled,
                "schedule_minutes": rule.schedule_minutes,
                "chat_id": rule.chat_id or chat_id,
                "source": getattr(rule, "source", "x"),
            }
        elif isinstance(rule, TrendWatch):
            kind = "trend"
            data = {
                "kind": kind,
                "rule_id": rule.rule_id,
                "label": rule.label,
                "category": rule.category,
                "domains": list(rule.domains),
                "enabled": rule.enabled,
                "schedule_minutes": rule.schedule_minutes,
                "chat_id": rule.chat_id or chat_id,
                "source": getattr(rule, "source", "x"),
            }
        else:
            raise TypeError(f"Unknown WatchRule type: {type(rule)}")

        return self.push("save_rule", data, chat_id=chat_id)

    def pop_pending(self, limit: int = 20) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, chat_id, action, payload FROM sns_requests "
                "WHERE status='pending' ORDER BY id LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            {"id": r[0], "chat_id": r[1], "action": r[2], "payload": json.loads(r[3])}
            for r in rows
        ]

    def mark_done(self, req_id: int) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE sns_requests SET status='done', processed_at=? WHERE id=?",
                (_utc_now(), req_id),
            )
            conn.commit()

    def mark_error(self, req_id: int, msg: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE sns_requests SET status='error', processed_at=?, payload=? WHERE id=?",
                (_utc_now(), json.dumps({"error": msg[:500]}), req_id),
            )
            conn.commit()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

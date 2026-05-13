from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from hashlib import sha1
from pathlib import Path
from typing import Iterator

from .models import (
    AccountWatch,
    KeywordWatch,
    TrendSnapshot,
    TrendWatch,
    Tweet,
    WatchKind,
    WatchRule,
    utc_now,
)


class SnsDatabase:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(str(self.path))
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    def bootstrap(self) -> None:
        with self.connect() as conn:
            conn.execute("PRAGMA foreign_keys = ON")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS watch_rules (
                    rule_id      TEXT PRIMARY KEY,
                    kind         TEXT NOT NULL,
                    label        TEXT NOT NULL,
                    query_json   TEXT NOT NULL,
                    enabled      INTEGER NOT NULL DEFAULT 1,
                    schedule_minutes INTEGER NOT NULL,
                    chat_id      TEXT NOT NULL,
                    last_checked_at TEXT,
                    created_at   TEXT NOT NULL,
                    updated_at   TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS seen_tweets (
                    tweet_id     TEXT NOT NULL,
                    rule_id      TEXT NOT NULL,
                    author_handle TEXT NOT NULL,
                    text         TEXT NOT NULL,
                    created_at   TEXT NOT NULL,
                    notified     INTEGER NOT NULL DEFAULT 0,
                    first_seen_at TEXT NOT NULL,
                    PRIMARY KEY (tweet_id, rule_id),
                    FOREIGN KEY (rule_id) REFERENCES watch_rules(rule_id) ON DELETE CASCADE
                );

                CREATE TABLE IF NOT EXISTS trend_snapshots (
                    snapshot_id  TEXT PRIMARY KEY,
                    rule_id      TEXT NOT NULL,
                    names_json   TEXT NOT NULL,
                    captured_at  TEXT NOT NULL,
                    FOREIGN KEY (rule_id) REFERENCES watch_rules(rule_id) ON DELETE CASCADE
                );
                """
            )
            conn.commit()

    def save_watch_rule(self, rule: WatchRule) -> None:
        with self.connect() as conn:
            now = utc_now().isoformat()
            kind = self._rule_kind(rule)
            query_json = self._rule_to_json(rule)

            conn.execute(
                """
                INSERT OR REPLACE INTO watch_rules
                (rule_id, kind, label, query_json, enabled, schedule_minutes, chat_id, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    rule.rule_id,
                    kind,
                    rule.label,
                    query_json,
                    1 if rule.enabled else 0,
                    rule.schedule_minutes,
                    rule.chat_id,
                    rule.last_checked_at.isoformat() if hasattr(rule, "last_checked_at") and rule.last_checked_at else now,
                    now,
                ),
            )
            conn.commit()

    def delete_watch_rule(self, rule_id: str) -> bool:
        with self.connect() as conn:
            cursor = conn.execute("DELETE FROM watch_rules WHERE rule_id = ?", (rule_id,))
            conn.commit()
            return cursor.rowcount > 0

    def toggle_watch_rule(self, rule_id: str, *, enabled: bool) -> bool:
        with self.connect() as conn:
            now = utc_now().isoformat()
            cursor = conn.execute(
                "UPDATE watch_rules SET enabled = ?, updated_at = ? WHERE rule_id = ?",
                (1 if enabled else 0, now, rule_id),
            )
            conn.commit()
            return cursor.rowcount > 0

    def list_watch_rules(self, kind: WatchKind | None = None) -> list[WatchRule]:
        with self.connect() as conn:
            if kind:
                rows = conn.execute(
                    "SELECT * FROM watch_rules WHERE kind = ? ORDER BY created_at",
                    (kind,),
                ).fetchall()
            else:
                rows = conn.execute("SELECT * FROM watch_rules ORDER BY created_at").fetchall()

            rules = []
            for row in rows:
                rule = self._row_to_rule(dict(row))
                if rule:
                    rules.append(rule)
            return rules

    def get_watch_rule(self, rule_id: str) -> WatchRule | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM watch_rules WHERE rule_id = ?", (rule_id,)).fetchone()
            if not row:
                return None
            return self._row_to_rule(dict(row))

    def update_user_id(self, rule_id: str, user_id: str) -> None:
        with self.connect() as conn:
            rule = self.get_watch_rule(rule_id)
            if not isinstance(rule, AccountWatch):
                return

            updated_rule = AccountWatch(
                rule_id=rule.rule_id,
                screen_name=rule.screen_name,
                user_id=user_id,
                label=rule.label,
                enabled=rule.enabled,
                schedule_minutes=rule.schedule_minutes,
                chat_id=rule.chat_id,
                last_checked_at=rule.last_checked_at,
            )
            self.save_watch_rule(updated_rule)

    def mark_rule_checked(self, rule_id: str) -> None:
        with self.connect() as conn:
            now = utc_now().isoformat()
            conn.execute(
                "UPDATE watch_rules SET last_checked_at = ?, updated_at = ? WHERE rule_id = ?",
                (now, now, rule_id),
            )
            conn.commit()

    def record_tweets(self, rule_id: str, tweets: list[Tweet]) -> list[Tweet]:
        """Insert tweets and return only newly seen ones. First check marks all as notified."""
        with self.connect() as conn:
            rule = self.get_watch_rule(rule_id)
            is_first_check = rule and rule.last_checked_at is None

            now = utc_now().isoformat()
            new_tweets = []

            for tweet in tweets:
                try:
                    conn.execute(
                        """
                        INSERT INTO seen_tweets
                        (tweet_id, rule_id, author_handle, text, created_at, notified, first_seen_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            tweet.tweet_id,
                            rule_id,
                            tweet.author_handle,
                            tweet.text,
                            tweet.created_at.isoformat(),
                            1 if is_first_check else 0,
                            now,
                        ),
                    )
                    if not is_first_check:
                        new_tweets.append(tweet)
                except sqlite3.IntegrityError:
                    pass

            conn.commit()
            return new_tweets

    def mark_tweets_notified(self, rule_id: str, tweet_ids: list[str]) -> None:
        with self.connect() as conn:
            for tweet_id in tweet_ids:
                conn.execute(
                    "UPDATE seen_tweets SET notified = 1 WHERE tweet_id = ? AND rule_id = ?",
                    (tweet_id, rule_id),
                )
            conn.commit()

    def save_trend_snapshot(self, snapshot: TrendSnapshot) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO trend_snapshots (snapshot_id, rule_id, names_json, captured_at)
                VALUES (?, ?, ?, ?)
                """,
                (
                    snapshot.snapshot_id,
                    snapshot.rule_id,
                    json.dumps(snapshot.names),
                    snapshot.captured_at.isoformat(),
                ),
            )
            conn.commit()

    def latest_trend_snapshot(self, rule_id: str) -> TrendSnapshot | None:
        with self.connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM trend_snapshots
                WHERE rule_id = ?
                ORDER BY captured_at DESC
                LIMIT 1
                """,
                (rule_id,),
            ).fetchone()

            if not row:
                return None

            row_dict = dict(row)
            return TrendSnapshot(
                snapshot_id=row_dict["snapshot_id"],
                rule_id=row_dict["rule_id"],
                names=tuple(json.loads(row_dict["names_json"])),
                captured_at=datetime.fromisoformat(row_dict["captured_at"]),
            )

    @staticmethod
    def _watch_rule_id(kind: str, key: str) -> str:
        """Generate deterministic rule ID from kind and key."""
        h = sha1(f"{kind}|{key}".encode()).hexdigest()
        return f"{kind}_{h[:12]}"

    @staticmethod
    def _snapshot_id(rule_id: str, captured_at_iso: str) -> str:
        """Generate deterministic snapshot ID."""
        h = sha1(f"{rule_id}|{captured_at_iso}".encode()).hexdigest()
        return h[:16]

    @staticmethod
    def _rule_kind(rule: WatchRule) -> WatchKind:
        if isinstance(rule, AccountWatch):
            return "account"
        elif isinstance(rule, KeywordWatch):
            return "keyword"
        elif isinstance(rule, TrendWatch):
            return "trend"
        raise ValueError(f"Unknown rule type: {type(rule)}")

    @staticmethod
    def _rule_to_json(rule: WatchRule) -> str:
        if isinstance(rule, AccountWatch):
            return json.dumps(
                {
                    "screen_name": rule.screen_name,
                    "user_id": rule.user_id,
                }
            )
        elif isinstance(rule, KeywordWatch):
            return json.dumps(
                {
                    "query": rule.query,
                }
            )
        elif isinstance(rule, TrendWatch):
            return json.dumps(
                {
                    "category": rule.category,
                }
            )
        raise ValueError(f"Unknown rule type: {type(rule)}")

    @staticmethod
    def _row_to_rule(row: dict) -> WatchRule | None:
        kind = row["kind"]
        query = json.loads(row["query_json"])
        last_checked = None
        if row["last_checked_at"]:
            last_checked = datetime.fromisoformat(row["last_checked_at"])

        if kind == "account":
            return AccountWatch(
                rule_id=row["rule_id"],
                screen_name=query["screen_name"],
                user_id=query.get("user_id"),
                label=row["label"],
                enabled=bool(row["enabled"]),
                schedule_minutes=row["schedule_minutes"],
                chat_id=row["chat_id"],
                last_checked_at=last_checked,
            )
        elif kind == "keyword":
            return KeywordWatch(
                rule_id=row["rule_id"],
                query=query["query"],
                label=row["label"],
                enabled=bool(row["enabled"]),
                schedule_minutes=row["schedule_minutes"],
                chat_id=row["chat_id"],
                last_checked_at=last_checked,
            )
        elif kind == "trend":
            return TrendWatch(
                rule_id=row["rule_id"],
                category=query["category"],
                label=row["label"],
                enabled=bool(row["enabled"]),
                schedule_minutes=row["schedule_minutes"],
                chat_id=row["chat_id"],
                last_checked_at=last_checked,
            )
        return None

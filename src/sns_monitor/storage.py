from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta
from hashlib import sha1
from pathlib import Path
from typing import Iterator

logger = logging.getLogger(__name__)

from .filters import normalize_keyword_filters
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
                    updated_at   TEXT NOT NULL,
                    source       TEXT NOT NULL DEFAULT 'x'
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

                CREATE TABLE IF NOT EXISTS sns_post_feedback (
                    feedback_id   TEXT PRIMARY KEY,
                    tweet_id      TEXT NOT NULL,
                    rule_id       TEXT NOT NULL,
                    chat_id       TEXT NOT NULL,
                    feedback_kind TEXT NOT NULL,
                    feedback_at   TEXT NOT NULL,
                    FOREIGN KEY (rule_id) REFERENCES watch_rules(rule_id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_sns_post_feedback_rule
                    ON sns_post_feedback(rule_id);
                CREATE INDEX IF NOT EXISTS idx_sns_post_feedback_tweet
                    ON sns_post_feedback(tweet_id);
                CREATE INDEX IF NOT EXISTS idx_sns_post_feedback_rule_kind_at
                    ON sns_post_feedback(rule_id, feedback_kind, feedback_at);

                CREATE TABLE IF NOT EXISTS sns_post_signals (
                    tweet_id              TEXT NOT NULL,
                    rule_id               TEXT NOT NULL,
                    long_term_score       INTEGER NOT NULL,
                    arbitrage_score       INTEGER NOT NULL,
                    matched_products_json TEXT NOT NULL DEFAULT '[]',
                    matched_keywords_json TEXT NOT NULL DEFAULT '[]',
                    matched_entities_json TEXT NOT NULL DEFAULT '[]',
                    suggested_action      TEXT NOT NULL DEFAULT '',
                    rationale             TEXT NOT NULL DEFAULT '',
                    deadline              TEXT,
                    bypass_reason         TEXT NOT NULL DEFAULT 'none',
                    pushed                INTEGER NOT NULL DEFAULT 0,
                    classified_at         TEXT NOT NULL,
                    PRIMARY KEY (tweet_id, rule_id),
                    FOREIGN KEY (rule_id) REFERENCES watch_rules(rule_id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_sns_post_signals_long_term
                    ON sns_post_signals(long_term_score);
                CREATE INDEX IF NOT EXISTS idx_sns_post_signals_arbitrage
                    ON sns_post_signals(arbitrage_score);
                CREATE INDEX IF NOT EXISTS idx_sns_post_signals_pushed_at
                    ON sns_post_signals(pushed, classified_at);

                CREATE TABLE IF NOT EXISTS sns_auto_discovery_rejects (
                    screen_name   TEXT PRIMARY KEY,        -- lowercased handle
                    original_rule_id TEXT,                 -- rule_id before deletion
                    domains_json  TEXT NOT NULL DEFAULT '[]',
                    deleted_at    TEXT NOT NULL,
                    chat_id       TEXT NOT NULL DEFAULT ''
                );

                -- Polarity-aware feedback timeline for auto-discovery decisions.
                -- 👍 button writes polarity='positive'; deletion (button or
                -- /snsdelete) writes polarity='negative'. Used by future
                -- few-shot pools + observability ("how many auto-adds get
                -- kept by the user?").
                CREATE TABLE IF NOT EXISTS sns_auto_discovery_feedback (
                    feedback_id          TEXT PRIMARY KEY,
                    screen_name          TEXT NOT NULL,        -- lowercased handle
                    polarity             TEXT NOT NULL,        -- 'positive' | 'negative'
                    domains_json         TEXT NOT NULL DEFAULT '[]',
                    llm_confidence       REAL,
                    llm_actionable_score REAL,
                    chat_id              TEXT NOT NULL DEFAULT '',
                    feedback_at          TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_sns_disc_feedback_handle
                    ON sns_auto_discovery_feedback(screen_name);
                CREATE INDEX IF NOT EXISTS idx_sns_disc_feedback_at
                    ON sns_auto_discovery_feedback(feedback_at);

                -- Per-domain trust score for SNS auto-discovery. Each domain
                -- the user wants the bot to track (pokemon / yugioh / ws /
                -- union_arena / tcg) accumulates keep / reject counts and
                -- carries a monotonically-increasing actionable threshold:
                -- it can ratchet UP on rejections but never resets DOWN, so
                -- a noisy domain auto-tightens over time and never quietly
                -- unwinds back to a permissive state.
                CREATE TABLE IF NOT EXISTS sns_discovery_domain_trust (
                    domain               TEXT PRIMARY KEY,
                    keep_count           INTEGER NOT NULL DEFAULT 0,
                    reject_count         INTEGER NOT NULL DEFAULT 0,
                    actionable_threshold REAL NOT NULL DEFAULT 0.75,
                    first_seen_at        TEXT NOT NULL,
                    last_updated_at      TEXT NOT NULL
                );
                """
            )
            # Idempotent ALTER TABLE for older DBs.
            existing_cols = {row["name"] for row in conn.execute("PRAGMA table_info(watch_rules)")}
            if "source" not in existing_cols:
                conn.execute("ALTER TABLE watch_rules ADD COLUMN source TEXT NOT NULL DEFAULT 'x'")
            if "cooldown_until" not in existing_cols:
                conn.execute("ALTER TABLE watch_rules ADD COLUMN cooldown_until TEXT")
            if "is_auto_discovered" not in existing_cols:
                conn.execute(
                    "ALTER TABLE watch_rules ADD COLUMN is_auto_discovered INTEGER NOT NULL DEFAULT 0"
                )
                # Backfill: rules previously stamped with the sentinel
                # source='auto_discovery' carried both the platform *and* the
                # provenance signal in one field, which crippled monitor
                # dispatch (the source plugin registry only knows about real
                # platforms). Move those rules over to the new split: real
                # platform in `source`, provenance in `is_auto_discovered`.
                # All historical auto-discovery candidates came from X (the
                # discovery regex only matches twitter.com / x.com), so the
                # backfill targets 'x'.
                conn.execute(
                    "UPDATE watch_rules SET is_auto_discovered = 1, source = 'x' "
                    "WHERE source = 'auto_discovery'"
                )
            conn.commit()

    def save_watch_rule(self, rule: WatchRule) -> None:
        with self.connect() as conn:
            now = utc_now().isoformat()
            kind = self._rule_kind(rule)
            query_json = self._rule_to_json(rule)
            existing = conn.execute(
                "SELECT created_at, last_checked_at FROM watch_rules WHERE rule_id = ?",
                (rule.rule_id,),
            ).fetchone()
            created_at = existing["created_at"] if existing else now
            last_checked_at = (
                rule.last_checked_at.isoformat()
                if getattr(rule, "last_checked_at", None)
                else (existing["last_checked_at"] if existing else None)
            )

            conn.execute(
                """
                INSERT OR REPLACE INTO watch_rules
                (rule_id, kind, label, query_json, enabled, schedule_minutes, chat_id, last_checked_at, created_at, updated_at, source, is_auto_discovered)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    rule.rule_id,
                    kind,
                    rule.label,
                    query_json,
                    1 if rule.enabled else 0,
                    rule.schedule_minutes,
                    rule.chat_id,
                    last_checked_at,
                    created_at,
                    now,
                    getattr(rule, "source", "x"),
                    1 if getattr(rule, "is_auto_discovered", False) else 0,
                ),
            )
            conn.commit()

    def delete_watch_rule(self, rule_id: str) -> bool:
        with self.connect() as conn:
            # Before hard-deleting, capture auto-discovered rules so discovery
            # can avoid re-adding handles the user explicitly removed.
            row = conn.execute(
                "SELECT source, is_auto_discovered, label, query_json, chat_id "
                "FROM watch_rules WHERE rule_id = ?",
                (rule_id,),
            ).fetchone()
            negative_feedback_args: dict | None = None
            if row and row["is_auto_discovered"]:
                import json as _json
                try:
                    query = _json.loads(row["query_json"] or "{}")
                    screen_name = (query.get("screen_name") or "").lower()
                    domains_list = list(query.get("domains") or [])
                    chat_id = row["chat_id"] or ""
                    if screen_name:
                        conn.execute(
                            """
                            INSERT INTO sns_auto_discovery_rejects
                                (screen_name, original_rule_id, domains_json, deleted_at, chat_id)
                            VALUES (?, ?, ?, ?, ?)
                            ON CONFLICT(screen_name) DO UPDATE SET
                                original_rule_id = excluded.original_rule_id,
                                domains_json     = excluded.domains_json,
                                deleted_at       = excluded.deleted_at,
                                chat_id          = excluded.chat_id
                            """,
                            (screen_name, rule_id, _json.dumps(domains_list), utc_now().isoformat(), chat_id),
                        )
                        # Stage the polarity-aware feedback write to run AFTER
                        # the watch_rules DELETE — record_auto_discovery_feedback
                        # opens its own connection, so we can't nest it here.
                        negative_feedback_args = {
                            "screen_name": screen_name,
                            "domains": tuple(domains_list),
                            "chat_id": chat_id,
                        }
                except Exception:
                    logger.exception("delete_watch_rule: failed to record rejection for rule_id=%s", rule_id)
            cursor = conn.execute("DELETE FROM watch_rules WHERE rule_id = ?", (rule_id,))
            conn.commit()
        # Record polarity-aware feedback row + bump per-domain trust outside
        # the connection we just committed. ``record_auto_discovery_feedback``
        # manages its own transaction so a failure here doesn't roll back the
        # deletion above (which is the user-visible action).
        if negative_feedback_args is not None:
            try:
                self.record_auto_discovery_feedback(
                    polarity="negative",
                    **negative_feedback_args,
                )
            except Exception:
                logger.exception(
                    "delete_watch_rule: failed to record negative feedback for rule_id=%s",
                    rule_id,
                )
        return cursor.rowcount > 0

    def list_rejected_handles(self, *, days: int = 90) -> frozenset[str]:
        """Return handles deleted within the last `days` days (lowercased).

        Used by discover_tcg_sns_accounts to avoid re-adding recently-rejected
        auto-discovered accounts."""
        cutoff = (utc_now() - __import__("datetime").timedelta(days=days)).isoformat()
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT screen_name FROM sns_auto_discovery_rejects WHERE deleted_at >= ?",
                (cutoff,),
            ).fetchall()
        return frozenset(r["screen_name"] for r in rows)

    def auto_discovery_stats(self) -> dict:
        """Return survival statistics for auto-discovered accounts.

        Returns a dict with keys: total_added, total_rejected, survive_count,
        survive_rate (float 0-1)."""
        with self.connect() as conn:
            total_added = conn.execute(
                "SELECT COUNT(*) FROM watch_rules WHERE is_auto_discovered = 1"
            ).fetchone()[0]
            total_rejected = conn.execute(
                "SELECT COUNT(*) FROM sns_auto_discovery_rejects"
            ).fetchone()[0]
        survive_count = total_added
        total = survive_count + total_rejected
        return {
            "total_added": total_added,
            "total_rejected": total_rejected,
            "survive_count": survive_count,
            "survive_rate": survive_count / total if total else 1.0,
        }

    # ── Auto-discovery feedback + per-domain trust ────────────────────────
    DEFAULT_DISCOVERY_ACTIONABLE_THRESHOLD: float = 0.75
    DISCOVERY_TIGHTENING_STEP: float = 0.05
    DISCOVERY_MAX_THRESHOLD: float = 0.95

    def record_auto_discovery_feedback(
        self,
        *,
        screen_name: str,
        polarity: str,
        domains: tuple[str, ...] = (),
        llm_confidence: float | None = None,
        llm_actionable_score: float | None = None,
        chat_id: str = "",
    ) -> str:
        """Append one feedback row + bump per-domain trust. Returns the
        feedback_id. ``polarity`` must be ``'positive'`` or ``'negative'``;
        anything else raises ValueError so silent typos do not pollute the
        learning signal."""
        if polarity not in {"positive", "negative"}:
            raise ValueError(f"polarity must be 'positive' or 'negative'; got {polarity!r}")
        screen_norm = (screen_name or "").lstrip("@").lower()
        if not screen_norm:
            raise ValueError("screen_name must be non-empty")
        now = utc_now().isoformat()
        feedback_id = sha1(
            f"{screen_norm}|{polarity}|{now}".encode("utf-8")
        ).hexdigest()
        domains_norm = tuple(d for d in (domains or ()) if d)
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO sns_auto_discovery_feedback
                    (feedback_id, screen_name, polarity, domains_json,
                     llm_confidence, llm_actionable_score, chat_id, feedback_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    feedback_id,
                    screen_norm,
                    polarity,
                    json.dumps(list(domains_norm)),
                    llm_confidence,
                    llm_actionable_score,
                    chat_id or "",
                    now,
                ),
            )
            self._bump_discovery_domain_trust_locked(
                conn,
                domains=domains_norm,
                kept=(polarity == "positive"),
                now=now,
            )
            conn.commit()
        return feedback_id

    def _bump_discovery_domain_trust_locked(
        self,
        conn: sqlite3.Connection,
        *,
        domains: tuple[str, ...],
        kept: bool,
        now: str,
    ) -> None:
        """Inner mutation — caller owns the connection + commit. For each
        domain row: bump keep_count or reject_count, and on rejection bump
        actionable_threshold by DISCOVERY_TIGHTENING_STEP capped at
        DISCOVERY_MAX_THRESHOLD. Threshold is monotonically increasing —
        positive feedback never lowers it."""
        for domain in domains:
            existing = conn.execute(
                "SELECT keep_count, reject_count, actionable_threshold "
                "FROM sns_discovery_domain_trust WHERE domain = ?",
                (domain,),
            ).fetchone()
            if existing is None:
                conn.execute(
                    "INSERT INTO sns_discovery_domain_trust "
                    "(domain, keep_count, reject_count, actionable_threshold, "
                    "first_seen_at, last_updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        domain,
                        1 if kept else 0,
                        0 if kept else 1,
                        self.DEFAULT_DISCOVERY_ACTIONABLE_THRESHOLD if kept
                        else min(
                            self.DISCOVERY_MAX_THRESHOLD,
                            self.DEFAULT_DISCOVERY_ACTIONABLE_THRESHOLD
                            + self.DISCOVERY_TIGHTENING_STEP,
                        ),
                        now,
                        now,
                    ),
                )
                continue
            keep_count = existing["keep_count"] + (1 if kept else 0)
            reject_count = existing["reject_count"] + (0 if kept else 1)
            if kept:
                new_threshold = existing["actionable_threshold"]
            else:
                new_threshold = min(
                    self.DISCOVERY_MAX_THRESHOLD,
                    existing["actionable_threshold"] + self.DISCOVERY_TIGHTENING_STEP,
                )
            conn.execute(
                "UPDATE sns_discovery_domain_trust "
                "SET keep_count = ?, reject_count = ?, "
                "    actionable_threshold = ?, last_updated_at = ? "
                "WHERE domain = ?",
                (keep_count, reject_count, new_threshold, now, domain),
            )

    def effective_actionable_threshold(
        self,
        domains,
        *,
        default: float | None = None,
    ) -> float:
        """Return the strictest (max) actionable threshold across the given
        candidate domains. Domains the system has not yet seen contribute
        ``default`` (cold-start floor). Used by auto-discovery to decide
        whether a candidate's LLM-supplied actionable score is high enough
        to clear the per-domain bar."""
        default_value = (
            default if default is not None else self.DEFAULT_DISCOVERY_ACTIONABLE_THRESHOLD
        )
        domain_list = [d for d in (domains or ()) if d]
        if not domain_list:
            return default_value
        with self.connect() as conn:
            placeholders = ",".join(["?"] * len(domain_list))
            rows = conn.execute(
                f"SELECT domain, actionable_threshold FROM sns_discovery_domain_trust "
                f"WHERE domain IN ({placeholders})",
                tuple(domain_list),
            ).fetchall()
        threshold_map = {row["domain"]: row["actionable_threshold"] for row in rows}
        return max(
            (threshold_map.get(d, default_value) for d in domain_list),
            default=default_value,
        )

    def auto_discovery_feedback_summary(self) -> dict:
        """Returns aggregate per-polarity counts + per-domain threshold map.
        For observability — surfaces how the discovery system has tightened
        over time without callers having to query the tables directly."""
        with self.connect() as conn:
            counts: dict[str, int] = {}
            for row in conn.execute(
                "SELECT polarity, COUNT(*) AS c FROM sns_auto_discovery_feedback "
                "GROUP BY polarity"
            ).fetchall():
                counts[row["polarity"]] = int(row["c"])
            domain_rows = conn.execute(
                "SELECT domain, keep_count, reject_count, actionable_threshold "
                "FROM sns_discovery_domain_trust ORDER BY domain"
            ).fetchall()
        return {
            "positive_count": counts.get("positive", 0),
            "negative_count": counts.get("negative", 0),
            "per_domain": [
                {
                    "domain": row["domain"],
                    "keep_count": row["keep_count"],
                    "reject_count": row["reject_count"],
                    "actionable_threshold": row["actionable_threshold"],
                }
                for row in domain_rows
            ],
        }

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

    def list_watch_rules_missing_domains(self, *, limit: int | None = None) -> list[WatchRule]:
        """List enabled watch rules that have no domain tags yet (used by the
        opportunity agent's one-rule-per-tick LLM backfill).
        """
        rules = [rule for rule in self.list_watch_rules() if rule.enabled and not rule.domains]
        if limit is not None:
            rules = rules[:limit]
        return rules

    def update_user_id(self, rule_id: str, user_id: str) -> None:
        from dataclasses import replace

        with self.connect() as conn:
            rule = self.get_watch_rule(rule_id)
            if not isinstance(rule, AccountWatch):
                return

            self.save_watch_rule(replace(rule, user_id=user_id))

    def mark_rule_checked(self, rule_id: str) -> None:
        with self.connect() as conn:
            now = utc_now().isoformat()
            conn.execute(
                "UPDATE watch_rules SET last_checked_at = ?, updated_at = ? WHERE rule_id = ?",
                (now, now, rule_id),
            )
            conn.commit()

    # ── SNS post feedback CRUD ──────────────────────────────────────────────

    def record_sns_post_feedback(
        self,
        *,
        tweet_id: str,
        rule_id: str,
        chat_id: str,
        feedback_kind: str,
    ) -> str:
        """Insert a feedback row. Returns the generated feedback_id.

        The feedback_id is hashed from (tweet_id, rule_id, feedback_at) so
        repeated taps on the same (tweet, rule) get distinct rows — we need
        time-series counting (for the 3-strike auto-disable rule), not
        last-write-wins.
        """
        now = utc_now().isoformat()
        feedback_id = sha1(
            f"{tweet_id}|{rule_id}|{now}|{feedback_kind}".encode("utf-8")
        ).hexdigest()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO sns_post_feedback
                    (feedback_id, tweet_id, rule_id, chat_id, feedback_kind, feedback_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (feedback_id, tweet_id, rule_id, chat_id, feedback_kind, now),
            )
            conn.commit()
        return feedback_id

    def count_recent_post_feedback(
        self, *, rule_id: str, feedback_kind: str, since_iso: str,
    ) -> int:
        """Count feedback rows for this rule with the given kind and
        feedback_at >= since_iso. Powers the 3-strike auto-disable rule."""
        with self.connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS n FROM sns_post_feedback "
                "WHERE rule_id = ? AND feedback_kind = ? AND feedback_at >= ?",
                (rule_id, feedback_kind, since_iso),
            ).fetchone()
        return int(row["n"]) if row else 0

    def feedback_counts_for_rule(
        self, *, rule_id: str, since_iso: str,
    ) -> dict[str, int]:
        """Aggregate counts grouped by feedback_kind. Used for the notification
        footer (📊 此帳號累計：👍 N / 👎 M / 💰 K)."""
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT feedback_kind, COUNT(*) AS n FROM sns_post_feedback "
                "WHERE rule_id = ? AND feedback_at >= ? GROUP BY feedback_kind",
                (rule_id, since_iso),
            ).fetchall()
        return {row["feedback_kind"]: int(row["n"]) for row in rows}

    def set_rule_cooldown(self, rule_id: str, until_iso: str | None) -> bool:
        with self.connect() as conn:
            now = utc_now().isoformat()
            cursor = conn.execute(
                "UPDATE watch_rules SET cooldown_until = ?, updated_at = ? "
                "WHERE rule_id = ?",
                (until_iso, now, rule_id),
            )
            conn.commit()
            return cursor.rowcount > 0

    def update_rule_schedule(self, rule_id: str, schedule_minutes: int) -> bool:
        """Set schedule_minutes on a rule. Clamps to [1, 1440] defensively."""
        clamped = max(1, min(1440, int(schedule_minutes)))
        with self.connect() as conn:
            now = utc_now().isoformat()
            cursor = conn.execute(
                "UPDATE watch_rules SET schedule_minutes = ?, updated_at = ? "
                "WHERE rule_id = ?",
                (clamped, now, rule_id),
            )
            conn.commit()
            return cursor.rowcount > 0

    # ── SNS signal classifications (RAG / two-opportunity classifier) ───────

    def record_sns_signal(
        self,
        *,
        tweet_id: str,
        rule_id: str,
        long_term_score: int,
        arbitrage_score: int,
        matched_products: tuple[str, ...] = (),
        matched_keywords: tuple[str, ...] = (),
        matched_entities: tuple[str, ...] = (),
        suggested_action: str = "",
        rationale: str = "",
        deadline: str | None = None,
        bypass_reason: str = "none",
        pushed: bool = False,
    ) -> None:
        """Upsert a classification result. Caller fires this regardless of
        whether the tweet was pushed — non-pushed rows are valuable for
        future ``/digest`` and threshold tuning."""
        now = utc_now().isoformat()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO sns_post_signals (
                    tweet_id, rule_id, long_term_score, arbitrage_score,
                    matched_products_json, matched_keywords_json, matched_entities_json,
                    suggested_action, rationale, deadline, bypass_reason, pushed,
                    classified_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(tweet_id, rule_id) DO UPDATE SET
                    long_term_score = excluded.long_term_score,
                    arbitrage_score = excluded.arbitrage_score,
                    matched_products_json = excluded.matched_products_json,
                    matched_keywords_json = excluded.matched_keywords_json,
                    matched_entities_json = excluded.matched_entities_json,
                    suggested_action = excluded.suggested_action,
                    rationale = excluded.rationale,
                    deadline = excluded.deadline,
                    bypass_reason = excluded.bypass_reason,
                    pushed = excluded.pushed,
                    classified_at = excluded.classified_at
                """,
                (
                    tweet_id, rule_id,
                    int(long_term_score), int(arbitrage_score),
                    json.dumps(list(matched_products), ensure_ascii=False),
                    json.dumps(list(matched_keywords), ensure_ascii=False),
                    json.dumps(list(matched_entities), ensure_ascii=False),
                    suggested_action or "", rationale or "", deadline,
                    bypass_reason, 1 if pushed else 0, now,
                ),
            )
            conn.commit()

    def get_sns_signal(self, *, tweet_id: str, rule_id: str) -> dict[str, object] | None:
        """Fetch the cached classification for a (tweet, rule) pair if any.
        Used by the monitor to skip re-classifying within the same tick."""
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM sns_post_signals WHERE tweet_id = ? AND rule_id = ?",
                (tweet_id, rule_id),
            ).fetchone()
        if row is None:
            return None
        return dict(row)

    def list_unpushed_signals_with_score(
        self, *, min_score: int = 60, limit: int = 50,
    ) -> list[dict[str, object]]:
        """For a future ``/digest`` view: borderline-but-dropped tweets."""
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT s.*, t.text, t.author_handle, t.created_at
                FROM sns_post_signals s
                LEFT JOIN seen_tweets t USING (tweet_id, rule_id)
                WHERE s.pushed = 0
                  AND (s.long_term_score >= ? OR s.arbitrage_score >= ?)
                ORDER BY s.classified_at DESC
                LIMIT ?
                """,
                (int(min_score), int(min_score), int(limit)),
            ).fetchall()
        return [dict(r) for r in rows]

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
    def _watch_rule_id(kind: str, key: str, source: str = "x") -> str:
        """Generate deterministic rule ID from kind and key.

        source="x" preserves the legacy hash format so existing rule IDs in
        the DB remain stable across this migration. Other sources prepend
        the source name to both the hash payload and the visible prefix so
        e.g. `keyword:Umbreon` doesn't collide between X and Reddit.
        """
        if source == "x":
            h = sha1(f"{kind}|{key}".encode()).hexdigest()
            return f"{kind}_{h[:12]}"
        h = sha1(f"{source}|{kind}|{key}".encode()).hexdigest()
        return f"{source}_{kind}_{h[:12]}"

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
                    "include_keywords": list(rule.include_keywords),
                    "domains": list(rule.domains),
                }
            )
        elif isinstance(rule, KeywordWatch):
            return json.dumps(
                {
                    "query": rule.query,
                    "domains": list(rule.domains),
                }
            )
        elif isinstance(rule, TrendWatch):
            return json.dumps(
                {
                    "category": rule.category,
                    "domains": list(rule.domains),
                }
            )
        raise ValueError(f"Unknown rule type: {type(rule)}")

    @staticmethod
    def _row_to_rule(row: dict) -> WatchRule | None:
        from .models import normalize_domains

        kind = row["kind"]
        query = json.loads(row["query_json"])
        last_checked = None
        if row["last_checked_at"]:
            last_checked = datetime.fromisoformat(row["last_checked_at"])

        domains = normalize_domains(query.get("domains"))
        source = row["source"] if "source" in row.keys() and row["source"] else "x"
        cooldown_until = row["cooldown_until"] if "cooldown_until" in row.keys() else None
        is_auto_discovered = bool(row["is_auto_discovered"]) if "is_auto_discovered" in row.keys() else False

        if kind == "account":
            return AccountWatch(
                rule_id=row["rule_id"],
                screen_name=query["screen_name"],
                user_id=query.get("user_id"),
                label=row["label"],
                include_keywords=normalize_keyword_filters(query.get("include_keywords")),
                domains=domains,
                enabled=bool(row["enabled"]),
                schedule_minutes=row["schedule_minutes"],
                chat_id=row["chat_id"],
                last_checked_at=last_checked,
                source=source,
                cooldown_until=cooldown_until,
                is_auto_discovered=is_auto_discovered,
            )
        elif kind == "keyword":
            return KeywordWatch(
                rule_id=row["rule_id"],
                query=query["query"],
                label=row["label"],
                domains=domains,
                enabled=bool(row["enabled"]),
                schedule_minutes=row["schedule_minutes"],
                chat_id=row["chat_id"],
                last_checked_at=last_checked,
                source=source,
                cooldown_until=cooldown_until,
            )
        elif kind == "trend":
            return TrendWatch(
                rule_id=row["rule_id"],
                category=query["category"],
                label=row["label"],
                domains=domains,
                enabled=bool(row["enabled"]),
                schedule_minutes=row["schedule_minutes"],
                chat_id=row["chat_id"],
                last_checked_at=last_checked,
                source=source,
                cooldown_until=cooldown_until,
            )
        return None

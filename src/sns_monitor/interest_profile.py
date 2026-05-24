"""Aggregate the three sources of explicit user-interest signal into one
dataclass the SNS signal classifier injects into its prompt.

Sources (in priority order):
  1. ``marketplace_watchlist`` (Mercari / Rakuma / future markets the user
     opted to monitor) — strongest "I care about exactly this" signal.
  2. ``opportunity_candidates.is_target = 1`` (candidates the user pinned
     via ``/hunt pin``) — explicit interest in the hunting pipeline.
  3. ``sns_post_feedback`` 30-day aggregate per rule — implicit signal from
     👍 / 👎 / 💰 reactions.

All three live in separate SQLite files (different repos / pipelines), so
this module reads them directly by path rather than coupling to in-process
DB objects. Read-only — never writes. Failures on any source degrade
gracefully (empty tuple / dict).
"""

from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class UserInterestProfile:
    """Snapshot of what the user has told us they care about.

    Empty fields mean "no signal yet" — the classifier should treat that as
    'unconstrained' (don't penalise novel topics) rather than 'reject all'.
    """
    chat_id: str
    watchlist_queries: tuple[str, ...] = ()
    pinned_targets: tuple[str, ...] = ()
    feedback_by_rule: dict[str, dict[str, int]] = field(default_factory=dict)
    built_at: str = ""

    def is_empty(self) -> bool:
        return not (self.watchlist_queries or self.pinned_targets or self.feedback_by_rule)


@contextmanager
def _read_only_conn(path: Path) -> Iterator[sqlite3.Connection | None]:
    """Open the DB in read-only mode via URI. Yields None if the file is
    missing — callers should handle that as a 'source unavailable' degraded
    state, not an error."""
    if not path.exists():
        yield None
        return
    uri = f"file:{path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _query_watchlist(monitor_db_path: Path, chat_id: str) -> tuple[str, ...]:
    """Pull active marketplace_watchlist queries for this chat_id.

    Filters to enabled=1 so paused watches don't show up in the profile.
    Returns deduped query strings, order-preserved by recency.
    """
    try:
        with _read_only_conn(monitor_db_path) as conn:
            if conn is None:
                return ()
            try:
                rows = conn.execute(
                    "SELECT DISTINCT query FROM marketplace_watchlist "
                    "WHERE enabled = 1 AND chat_id = ? ORDER BY updated_at DESC",
                    (str(chat_id),),
                ).fetchall()
            except sqlite3.OperationalError:
                # Marketplace tables not yet migrated; just return empty.
                return ()
    except sqlite3.Error:
        logger.exception("interest_profile: marketplace_watchlist query failed path=%s", monitor_db_path)
        return ()
    return tuple(r["query"] for r in rows if r["query"])


def _query_pinned_targets(opportunity_db_path: Path, chat_id: str | None = None) -> tuple[str, ...]:
    """Pull pinned target titles from opportunity_candidates.is_target=1.

    chat_id filtering isn't applied because opportunity_candidates is shared
    across chats in the current schema (target = global pin)."""
    try:
        with _read_only_conn(opportunity_db_path) as conn:
            if conn is None:
                return ()
            try:
                rows = conn.execute(
                    "SELECT DISTINCT title FROM opportunity_candidates "
                    "WHERE is_target = 1 AND status = 'active' ORDER BY updated_at DESC"
                ).fetchall()
            except sqlite3.OperationalError:
                return ()
    except sqlite3.Error:
        logger.exception("interest_profile: opportunity_candidates query failed path=%s", opportunity_db_path)
        return ()
    return tuple(r["title"] for r in rows if r["title"])


def _query_feedback_aggregates(
    sns_db_path: Path, chat_id: str, window_days: int = 30,
) -> dict[str, dict[str, int]]:
    """Group sns_post_feedback rows by (rule_id, feedback_kind) over the last
    window_days. Returns {rule_id: {up: N, down: M, bought: K}}."""
    since_iso = (
        datetime.now(timezone.utc) - timedelta(days=window_days)
    ).replace(microsecond=0).isoformat()
    out: dict[str, dict[str, int]] = {}
    try:
        with _read_only_conn(sns_db_path) as conn:
            if conn is None:
                return out
            try:
                rows = conn.execute(
                    "SELECT rule_id, feedback_kind, COUNT(*) AS n "
                    "FROM sns_post_feedback "
                    "WHERE chat_id = ? AND feedback_at >= ? "
                    "GROUP BY rule_id, feedback_kind",
                    (str(chat_id), since_iso),
                ).fetchall()
            except sqlite3.OperationalError:
                return out
    except sqlite3.Error:
        logger.exception("interest_profile: sns_post_feedback query failed path=%s", sns_db_path)
        return out
    for row in rows:
        rule_id = str(row["rule_id"])
        kind = str(row["feedback_kind"])
        out.setdefault(rule_id, {"up": 0, "down": 0, "bought": 0})[kind] = int(row["n"])
    return out


def build_user_interest_profile(
    *,
    chat_id: str,
    sns_db_path: str | Path,
    monitor_db_path: str | Path,
    opportunity_db_path: str | Path,
    window_days: int = 30,
) -> UserInterestProfile:
    """Aggregate the three signal sources. Each query degrades gracefully on
    missing DB / missing table / SQL error — the profile will simply contain
    fewer signals rather than raise."""
    return UserInterestProfile(
        chat_id=str(chat_id),
        watchlist_queries=_query_watchlist(Path(monitor_db_path), str(chat_id)),
        pinned_targets=_query_pinned_targets(Path(opportunity_db_path), str(chat_id)),
        feedback_by_rule=_query_feedback_aggregates(Path(sns_db_path), str(chat_id), window_days),
        built_at=_utc_now_iso(),
    )


def aggregate_rule_feedback(profile: UserInterestProfile, rule_id: str) -> dict[str, int]:
    """Look up a single rule's feedback counts; returns zeros if absent."""
    return dict(profile.feedback_by_rule.get(rule_id, {"up": 0, "down": 0, "bought": 0}))

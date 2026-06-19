"""Storage migration tests — `source` column adoption.

Verifies:
- bootstrap() on a fresh DB creates the column.
- bootstrap() on a legacy DB (no source column) ALTERs it in idempotently
  and existing rows default to source='x' so old rule_ids stay valid.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from sns_monitor.models import AccountWatch
from sns_monitor.storage import SnsDatabase


def test_bootstrap_creates_source_column_on_fresh_db(tmp_path: Path) -> None:
    db = SnsDatabase(tmp_path / "fresh.db")
    db.bootstrap()
    with sqlite3.connect(db.path) as conn:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(watch_rules)")}
    assert "source" in cols


def test_bootstrap_idempotent_when_source_column_already_exists(tmp_path: Path) -> None:
    db = SnsDatabase(tmp_path / "twice.db")
    db.bootstrap()
    db.bootstrap()  # second call must not raise (no double ALTER)
    with sqlite3.connect(db.path) as conn:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(watch_rules)")}
    assert "source" in cols


def test_bootstrap_alters_legacy_db_without_source_column(tmp_path: Path) -> None:
    legacy_path = tmp_path / "legacy.db"
    # Create a legacy schema by hand — no `source` column.
    with sqlite3.connect(legacy_path) as conn:
        conn.executescript(
            """
            CREATE TABLE watch_rules (
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
            INSERT INTO watch_rules VALUES (
                'account_abc12345', 'account', '@elonmusk',
                '{"screen_name":"elonmusk","user_id":null,"include_keywords":[],"domains":[]}',
                1, 15, '12345', NULL, '2025-01-01T00:00:00+00:00', '2025-01-01T00:00:00+00:00'
            );
            """
        )
        conn.commit()

    db = SnsDatabase(legacy_path)
    db.bootstrap()

    with sqlite3.connect(legacy_path) as conn:
        conn.row_factory = sqlite3.Row
        cols = {row[1] for row in conn.execute("PRAGMA table_info(watch_rules)")}
        assert "source" in cols
        row = conn.execute("SELECT * FROM watch_rules WHERE rule_id='account_abc12345'").fetchone()
        assert row["source"] == "x"  # backfilled default


def test_legacy_rule_id_format_preserved_for_x_source() -> None:
    """`source='x'` must produce the legacy rule_id format so existing rules
    don't orphan after migration. Only non-X sources get the new prefix."""
    legacy = SnsDatabase._watch_rule_id("account", "elonmusk")
    same_with_explicit_x = SnsDatabase._watch_rule_id("account", "elonmusk", "x")
    assert legacy == same_with_explicit_x

    # A non-X source string gets the new prefixed rule_id format. (The storage
    # layer stays source-agnostic even though Reddit itself has been removed.)
    other_id = SnsDatabase._watch_rule_id("account", "PokemonTCG", "4chan")
    assert other_id.startswith("4chan_account_")
    assert other_id != SnsDatabase._watch_rule_id("account", "PokemonTCG")


def test_save_and_load_non_x_watch_rule(tmp_path: Path) -> None:
    db = SnsDatabase(tmp_path / "other.db")
    db.bootstrap()
    rule = AccountWatch(
        rule_id=SnsDatabase._watch_rule_id("account", "PokemonTCG", "4chan"),
        screen_name="PokemonTCG",
        user_id="PokemonTCG",
        label="/vp/",
        include_keywords=(),
        domains=("pokemon",),
        enabled=True,
        schedule_minutes=30,
        chat_id="99999",
        last_checked_at=None,
        source="4chan",
    )
    db.save_watch_rule(rule)

    loaded = db.get_watch_rule(rule.rule_id)
    assert isinstance(loaded, AccountWatch)
    assert loaded.source == "4chan"
    assert loaded.screen_name == "PokemonTCG"
    assert loaded.schedule_minutes == 30

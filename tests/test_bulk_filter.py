from __future__ import annotations

from datetime import datetime, timezone

from sns_monitor.bulk_filter import (
    SCHEDULE_MAX_MINUTES,
    SCHEDULE_MIN_MINUTES,
    apply_bulk_keyword_filter_add,
    apply_bulk_keyword_filter_remove,
    apply_bulk_schedule_update,
    find_accounts_matching_domain,
    merge_keywords_dedupe,
    resolve_target_domain_set,
)
from sns_monitor.models import AccountWatch, KeywordWatch, TCG_DOMAINS
from sns_monitor.storage import SnsDatabase


def _account(handle: str, *, domains: tuple[str, ...] = (), keywords: tuple[str, ...] = ()) -> AccountWatch:
    return AccountWatch(
        rule_id=SnsDatabase._watch_rule_id("account", handle),
        screen_name=handle,
        user_id=None,
        label=f"@{handle}",
        include_keywords=keywords,
        domains=domains,
        enabled=True,
        schedule_minutes=15,
        chat_id="0",
        last_checked_at=None,
    )


def _keyword_watch(query: str, *, domains: tuple[str, ...] = ()) -> KeywordWatch:
    return KeywordWatch(
        rule_id=SnsDatabase._watch_rule_id("keyword", query),
        query=query,
        label=query,
        domains=domains,
        enabled=True,
        schedule_minutes=15,
        chat_id="0",
        last_checked_at=None,
    )


def test_resolve_tcg_returns_full_tcg_domains() -> None:
    assert resolve_target_domain_set("tcg") == TCG_DOMAINS
    assert resolve_target_domain_set("TCG") == TCG_DOMAINS
    assert resolve_target_domain_set(" tcg ") == TCG_DOMAINS


def test_resolve_specific_domain_returns_singleton() -> None:
    assert resolve_target_domain_set("pokemon") == frozenset({"pokemon"})
    assert resolve_target_domain_set("yugioh") == frozenset({"yugioh"})


def test_resolve_empty_returns_empty_frozenset() -> None:
    assert resolve_target_domain_set("") == frozenset()
    assert resolve_target_domain_set("   ") == frozenset()


def test_find_accounts_filters_by_domain_intersection(tmp_path) -> None:
    db = SnsDatabase(tmp_path / "sns.sqlite3")
    db.bootstrap()
    db.save_watch_rule(_account("poke_news", domains=("pokemon", "tcg")))
    db.save_watch_rule(_account("yugioh_jp", domains=("yugioh",)))
    db.save_watch_rule(_account("ws_news", domains=("ws",)))
    db.save_watch_rule(_account("politics_bot", domains=("politic",)))
    db.save_watch_rule(_account("untagged_one"))  # no domains

    matched = find_accounts_matching_domain(db, TCG_DOMAINS)
    handles = {r.screen_name for r in matched}
    assert handles == {"poke_news", "yugioh_jp", "ws_news"}


def test_find_accounts_ignores_keyword_watch_rules(tmp_path) -> None:
    """Bulk filter is scoped to account watches; keyword/trend watches are
    structurally different and must not appear in the result."""
    db = SnsDatabase(tmp_path / "sns.sqlite3")
    db.bootstrap()
    db.save_watch_rule(_account("acc1", domains=("pokemon",)))
    db.save_watch_rule(_keyword_watch("ピカチュウ", domains=("pokemon",)))

    matched = find_accounts_matching_domain(db, frozenset({"pokemon"}))
    assert len(matched) == 1
    assert matched[0].screen_name == "acc1"


def test_merge_keywords_dedupe_preserves_existing_order_and_casefolds() -> None:
    assert merge_keywords_dedupe(("抽選",), ("抽選", "新弾")) == ("抽選", "新弾")
    # case-insensitive
    assert merge_keywords_dedupe(("buy",), ("BUY", "Sell")) == ("buy", "Sell")
    # empty new tuple → unchanged
    assert merge_keywords_dedupe(("a", "b"), ()) == ("a", "b")
    # empty existing tuple
    assert merge_keywords_dedupe((), ("x", "y")) == ("x", "y")
    # internal dedupe in new sequence
    assert merge_keywords_dedupe((), ("a", "A", "b")) == ("a", "b")


def test_apply_bulk_skips_accounts_where_keyword_already_present(tmp_path) -> None:
    db = SnsDatabase(tmp_path / "sns.sqlite3")
    db.bootstrap()
    rule_with = _account("with_filter", domains=("pokemon",), keywords=("抽選",))
    rule_without = _account("without_filter", domains=("pokemon",))
    db.save_watch_rule(rule_with)
    db.save_watch_rule(rule_without)

    updated = apply_bulk_keyword_filter_add(
        db, [rule_with, rule_without], ["抽選"]
    )

    # Only the rule that didn't have "抽選" gets persisted as updated.
    assert {r.screen_name for r in updated} == {"without_filter"}
    # DB reflects the change.
    refreshed_without = db.get_watch_rule(rule_without.rule_id)
    assert refreshed_without.include_keywords == ("抽選",)
    # The rule that already had it is unchanged.
    refreshed_with = db.get_watch_rule(rule_with.rule_id)
    assert refreshed_with.include_keywords == ("抽選",)


def test_apply_bulk_preserves_other_fields(tmp_path) -> None:
    db = SnsDatabase(tmp_path / "sns.sqlite3")
    db.bootstrap()
    original = _account("acc1", domains=("pokemon", "tcg"))
    db.save_watch_rule(original)

    apply_bulk_keyword_filter_add(db, [original], ["抽選"])

    refreshed = db.get_watch_rule(original.rule_id)
    assert refreshed.include_keywords == ("抽選",)
    assert refreshed.domains == ("pokemon", "tcg")
    assert refreshed.screen_name == "acc1"
    assert refreshed.chat_id == "0"
    assert refreshed.schedule_minutes == 15


# ── apply_bulk_keyword_filter_remove ─────────────────────────────────────────


def test_apply_bulk_remove_drops_specific_keyword_only(tmp_path) -> None:
    db = SnsDatabase(tmp_path / "sns.sqlite3")
    db.bootstrap()
    rule = _account("acc1", domains=("tcg",), keywords=("720分鐘", "抽選", "新弾"))
    db.save_watch_rule(rule)

    updated = apply_bulk_keyword_filter_remove(db, [rule], ["720分鐘"])

    assert len(updated) == 1
    assert updated[0].include_keywords == ("抽選", "新弾")
    refreshed = db.get_watch_rule(rule.rule_id)
    assert refreshed.include_keywords == ("抽選", "新弾")


def test_apply_bulk_remove_skips_accounts_where_keyword_absent(tmp_path) -> None:
    db = SnsDatabase(tmp_path / "sns.sqlite3")
    db.bootstrap()
    rule_has = _account("has", domains=("tcg",), keywords=("720分鐘", "新弾"))
    rule_no = _account("no", domains=("tcg",), keywords=("抽選",))
    db.save_watch_rule(rule_has)
    db.save_watch_rule(rule_no)

    updated = apply_bulk_keyword_filter_remove(db, [rule_has, rule_no], ["720分鐘"])

    assert {r.screen_name for r in updated} == {"has"}
    refreshed_no = db.get_watch_rule(rule_no.rule_id)
    assert refreshed_no.include_keywords == ("抽選",)


def test_apply_bulk_remove_is_casefold(tmp_path) -> None:
    db = SnsDatabase(tmp_path / "sns.sqlite3")
    db.bootstrap()
    rule = _account("acc", domains=("tcg",), keywords=("Restock", "Other"))
    db.save_watch_rule(rule)

    updated = apply_bulk_keyword_filter_remove(db, [rule], ["RESTOCK"])

    assert updated[0].include_keywords == ("Other",)


def test_apply_bulk_remove_empty_keywords_is_noop(tmp_path) -> None:
    db = SnsDatabase(tmp_path / "sns.sqlite3")
    db.bootstrap()
    rule = _account("acc", domains=("tcg",), keywords=("a",))
    db.save_watch_rule(rule)

    assert apply_bulk_keyword_filter_remove(db, [rule], []) == []
    assert db.get_watch_rule(rule.rule_id).include_keywords == ("a",)


# ── apply_bulk_schedule_update ───────────────────────────────────────────────


def test_apply_bulk_schedule_update_changes_minutes(tmp_path) -> None:
    db = SnsDatabase(tmp_path / "sns.sqlite3")
    db.bootstrap()
    rule = _account("acc1", domains=("tcg",))
    db.save_watch_rule(rule)

    updated = apply_bulk_schedule_update(db, [rule], 720)

    assert len(updated) == 1
    assert updated[0].schedule_minutes == 720
    assert db.get_watch_rule(rule.rule_id).schedule_minutes == 720


def test_apply_bulk_schedule_update_clamps_to_valid_range(tmp_path) -> None:
    db = SnsDatabase(tmp_path / "sns.sqlite3")
    db.bootstrap()
    rule = _account("acc", domains=("tcg",))
    db.save_watch_rule(rule)

    apply_bulk_schedule_update(db, [rule], 99999)
    assert db.get_watch_rule(rule.rule_id).schedule_minutes == SCHEDULE_MAX_MINUTES

    apply_bulk_schedule_update(db, [db.get_watch_rule(rule.rule_id)], 0)
    assert db.get_watch_rule(rule.rule_id).schedule_minutes == SCHEDULE_MIN_MINUTES


def test_apply_bulk_schedule_skips_accounts_already_at_target_value(tmp_path) -> None:
    db = SnsDatabase(tmp_path / "sns.sqlite3")
    db.bootstrap()
    rule_at = _account("at", domains=("tcg",))
    rule_different = _account("diff", domains=("tcg",))
    db.save_watch_rule(rule_at)
    db.save_watch_rule(rule_different)
    # Set "at" to 720 first
    apply_bulk_schedule_update(db, [rule_at], 720)
    refreshed_at = db.get_watch_rule(rule_at.rule_id)

    # Now ask for 720 on both — only "diff" should change
    updated = apply_bulk_schedule_update(
        db, [refreshed_at, rule_different], 720
    )
    assert {r.screen_name for r in updated} == {"diff"}

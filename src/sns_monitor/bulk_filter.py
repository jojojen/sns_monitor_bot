"""Bulk-update helpers for SNS account watch rules.

Driven by the Telegram natural-language intent ``sns_bulk_add_filter`` —
e.g. "把每個跟 tcg 相關的 sns 追蹤帳號 filter 都加上「抽選」". The bot finds
matching accounts, previews them, and (on confirmation) calls
``apply_bulk_keyword_filter_add`` to merge new include_keywords into each
rule. All four helpers are pure functions so unit tests don't need the bot.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import replace

from .models import AccountWatch, TCG_DOMAINS
from .storage import SnsDatabase


def resolve_target_domain_set(target: str) -> frozenset[str]:
    """Normalise a user-supplied domain target string to a domain set.

    ``"tcg"`` is the umbrella term and expands to all TCG_DOMAINS (pokemon,
    yugioh, ws, union_arena, tcg). Any other value is treated as a single
    specific domain (e.g. ``"pokemon"`` → ``frozenset({"pokemon"})``).
    """
    cleaned = (target or "").strip().lower()
    if not cleaned:
        return frozenset()
    if cleaned == "tcg":
        return TCG_DOMAINS
    return frozenset({cleaned})


def find_accounts_matching_domain(
    sns_db: SnsDatabase, target_domains: frozenset[str]
) -> list[AccountWatch]:
    """Return all account-watch rules whose ``domains`` intersects ``target_domains``.

    Rules of other kinds (keyword / trend watches) are filtered out. Rules
    with empty / unknown domains are skipped — they're considered untagged
    and outside the scope of bulk-targeted updates.
    """
    if not target_domains:
        return []
    rules = sns_db.list_watch_rules(kind="account")
    matched: list[AccountWatch] = []
    for rule in rules:
        if not isinstance(rule, AccountWatch):
            continue
        if set(rule.domains) & target_domains:
            matched.append(rule)
    return matched


def merge_keywords_dedupe(
    existing: tuple[str, ...], new: Iterable[str]
) -> tuple[str, ...]:
    """Combine two keyword sequences, preserving order, dropping case-insensitive
    duplicates. The existing keywords keep their order at the front; new ones
    are appended only when not already present (case-fold compared)."""
    seen: dict[str, str] = {}
    out: list[str] = []
    for kw in existing:
        if not kw:
            continue
        key = kw.casefold()
        if key in seen:
            continue
        seen[key] = kw
        out.append(kw)
    for kw in new:
        if not kw:
            continue
        key = kw.casefold()
        if key in seen:
            continue
        seen[key] = kw
        out.append(kw)
    return tuple(out)


def apply_bulk_keyword_filter_add(
    sns_db: SnsDatabase,
    accounts: list[AccountWatch],
    keywords: Iterable[str],
) -> list[AccountWatch]:
    """Add ``keywords`` to each account's ``include_keywords`` and persist.

    Rules where all the new keywords are already present are skipped (no
    save, not returned in the updated list). Returns the list of rules that
    actually changed, in their *new* form.
    """
    keyword_tuple = tuple(keywords)
    updated: list[AccountWatch] = []
    for rule in accounts:
        merged = merge_keywords_dedupe(rule.include_keywords, keyword_tuple)
        if merged == rule.include_keywords:
            continue  # nothing new — skip
        new_rule = replace(rule, include_keywords=merged)
        sns_db.save_watch_rule(new_rule)
        updated.append(new_rule)
    return updated

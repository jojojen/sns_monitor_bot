"""Guardrail tests for SnsMonitor's classifier wiring.

These are the regression tests that protect the user's existing useful
features (explicit keyword filter + marketplace watchlist) — see the plan's
'Bypass A' / 'Bypass B' guardrails section.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sns_monitor.models import AccountWatch, Tweet
from sns_monitor.monitor import SnsMonitor


class _StubDB:
    """Minimal stand-in so we can exercise _classify_one_and_maybe_push
    without hitting sqlite. Records what would be persisted."""

    def __init__(self) -> None:
        self.recorded: list[dict] = []
        self.cached_signals: dict[tuple[str, str], dict] = {}

    def get_sns_signal(self, *, tweet_id, rule_id):
        return self.cached_signals.get((tweet_id, rule_id))

    def record_sns_signal(self, **kwargs):
        self.recorded.append(kwargs)


class _StubAliasSource:
    def all_aliases(self):
        return []
    def lookup_canonical(self, _alias):
        return None


def _make_monitor(*, llm_fn, min_score=60, alias_source=None):
    # SnsMonitor.__init__ does a SnsDatabase(db_path) call we need to bypass.
    # Easiest: construct partially-initialised instance and set attributes
    # the methods under test actually read.
    monitor = SnsMonitor.__new__(SnsMonitor)
    monitor._db = _StubDB()
    monitor._classifier_llm_fn = llm_fn
    monitor._entity_extraction_llm_fn = None
    monitor._alias_source = alias_source or _StubAliasSource()
    monitor._knowledge_retriever = None
    monitor._entity_research_fn = None
    monitor._ip_heat_retriever = None
    monitor._monitor_db_path = None
    monitor._opportunity_db_path = None
    from pathlib import Path
    monitor._db_path = Path("/tmp/test.sqlite3")
    monitor._min_score_to_push = min_score
    monitor._profile_cache = {}
    monitor._notify_fn = lambda *a, **kw: None
    return monitor


def _make_tweet(text: str = "おはよ") -> Tweet:
    return Tweet(
        tweet_id="t1", author_handle="alice", author_id="100",
        text=text, created_at=datetime(2026, 5, 23, 10, 0, tzinfo=timezone.utc),
        url="https://x.com/alice/status/t1",
    )


def _make_account_rule(*, include_keywords=()) -> AccountWatch:
    return AccountWatch(
        rule_id="r1", screen_name="alice", user_id=None, label="alice",
        include_keywords=include_keywords, chat_id="chat-A",
    )


# ── Bypass A: explicit keyword always pushes ────────────────────────────────


def test_explicit_keyword_match_always_pushes_even_when_classifier_says_noise():
    """User set rule.include_keywords=('抽選',). Tweet contains '抽選'. LLM
    says both scores 0. The user's keyword filter is the authority here —
    Bypass A must push anyway."""
    def noise_llm(_prompt):
        return '{"long_term_score": 0, "arbitrage_score": 0, "rationale": "雜訊"}'

    delivered_messages: list[tuple[str, str]] = []
    monitor = _make_monitor(llm_fn=noise_llm)
    monitor._notify_fn = lambda chat_id, text, reply_markup=None: delivered_messages.append((chat_id, text))

    rule = _make_account_rule(include_keywords=("抽選",))
    tweet = _make_tweet("アビスアイ 抽選販売")

    from sns_monitor.interest_profile import UserInterestProfile
    profile = UserInterestProfile(chat_id="chat-A")
    delivered: list[str] = []
    monitor._classify_one_and_maybe_push(
        rule=rule, tweet=tweet, profile=profile,
        feedback_for_rule={}, footer_counts={},
        force_bypass_keyword=None,
        delivered=delivered,
    )

    assert delivered == ["t1"], "explicit-keyword tweet must be pushed"
    assert len(delivered_messages) == 1
    chat_id, text = delivered_messages[0]
    assert chat_id == "chat-A"
    assert "命中你設的篩選關鍵字「抽選」" in text, "bypass header must be present"

    record = monitor._db.recorded[0]
    assert record["bypass_reason"] == "explicit_keyword"
    assert record["pushed"] == 1
    assert record["long_term_score"] == 0
    assert record["arbitrage_score"] == 0


def test_explicit_keyword_bypass_still_runs_classifier_for_record():
    """Bypass A skips the gate, not the classifier. Score must still be stored
    so /digest / future tuning has data."""
    captured = []
    def llm(prompt):
        captured.append(prompt)
        return '{"long_term_score": 10, "arbitrage_score": 5}'

    monitor = _make_monitor(llm_fn=llm)
    rule = _make_account_rule(include_keywords=("抽選",))
    tweet = _make_tweet("これは抽選の話")

    from sns_monitor.interest_profile import UserInterestProfile
    monitor._classify_one_and_maybe_push(
        rule=rule, tweet=tweet, profile=UserInterestProfile(chat_id="chat-A"),
        feedback_for_rule={}, footer_counts={}, force_bypass_keyword=None,
        delivered=[],
    )

    assert len(captured) == 1, "classifier must run even when bypass would fire"
    record = monitor._db.recorded[0]
    assert record["long_term_score"] == 10
    assert record["arbitrage_score"] == 5
    assert record["bypass_reason"] == "explicit_keyword"


# ── Score gate behaviour ────────────────────────────────────────────────────


def test_keywordless_rule_drops_below_threshold_and_persists_with_pushed_zero():
    """Rule has no include_keywords. Classifier returns weak signal. Monitor
    must NOT push but must still write the row so future /digest can review."""
    def llm(_prompt):
        return '{"long_term_score": 30, "arbitrage_score": 20}'

    monitor = _make_monitor(llm_fn=llm, min_score=60)
    rule = _make_account_rule(include_keywords=())
    tweet = _make_tweet("ただの日記です")

    notify_calls = []
    monitor._notify_fn = lambda *a, **kw: notify_calls.append(a)

    from sns_monitor.interest_profile import UserInterestProfile
    delivered: list[str] = []
    monitor._classify_one_and_maybe_push(
        rule=rule, tweet=tweet, profile=UserInterestProfile(chat_id="chat-A"),
        feedback_for_rule={}, footer_counts={},
        force_bypass_keyword=None, delivered=delivered,
    )

    assert delivered == [], "below-threshold tweet must NOT be pushed"
    assert notify_calls == []
    record = monitor._db.recorded[0]
    assert record["pushed"] == 0
    assert record["bypass_reason"] == "none"


def test_keywordless_rule_pushes_strong_long_term_signal():
    def llm(_prompt):
        return '{"long_term_score": 85, "arbitrage_score": 20, "suggested_action": "加入長期 watchlist"}'

    monitor = _make_monitor(llm_fn=llm, min_score=60)
    rule = _make_account_rule(include_keywords=())
    tweet = _make_tweet("アビスアイがEOL予告")

    delivered_messages: list[tuple[str, str]] = []
    monitor._notify_fn = lambda chat_id, text, reply_markup=None: delivered_messages.append((chat_id, text))

    from sns_monitor.interest_profile import UserInterestProfile
    delivered: list[str] = []
    monitor._classify_one_and_maybe_push(
        rule=rule, tweet=tweet, profile=UserInterestProfile(chat_id="chat-A"),
        feedback_for_rule={}, footer_counts={},
        force_bypass_keyword=None, delivered=delivered,
    )

    assert delivered == ["t1"]
    assert len(delivered_messages) == 1
    _chat_id, text = delivered_messages[0]
    assert "📈 長期潛力訊號" in text
    assert "命中你設的篩選關鍵字" not in text, "no bypass header when score gate fired"
    record = monitor._db.recorded[0]
    assert record["bypass_reason"] == "long_term"
    assert record["pushed"] == 1


def test_keywordless_rule_pushes_both_signals_when_both_high():
    def llm(_prompt):
        return '{"long_term_score": 80, "arbitrage_score": 80}'

    monitor = _make_monitor(llm_fn=llm, min_score=60)
    rule = _make_account_rule(include_keywords=())
    tweet = _make_tweet("BOX 抽選 + EOL")

    msgs = []
    monitor._notify_fn = lambda chat_id, text, reply_markup=None: msgs.append(text)

    from sns_monitor.interest_profile import UserInterestProfile
    monitor._classify_one_and_maybe_push(
        rule=rule, tweet=tweet, profile=UserInterestProfile(chat_id="chat-A"),
        feedback_for_rule={}, footer_counts={},
        force_bypass_keyword=None, delivered=[],
    )

    assert len(msgs) == 1
    assert "📈⚡ 雙重訊號" in msgs[0]


def test_keyword_watch_force_bypass_pushes_regardless_of_score():
    """KeywordWatch passes force_bypass_keyword=rule.query so the user's
    keyword search always shows results."""
    def llm(_prompt):
        return '{"long_term_score": 0, "arbitrage_score": 0}'

    monitor = _make_monitor(llm_fn=llm)
    from sns_monitor.models import KeywordWatch
    rule = KeywordWatch(rule_id="kw1", query="アビスアイ box", label="abyss", chat_id="chat-A")
    tweet = _make_tweet("アビスアイ box 新発売")

    msgs = []
    monitor._notify_fn = lambda chat_id, text, reply_markup=None: msgs.append(text)

    from sns_monitor.interest_profile import UserInterestProfile
    delivered: list[str] = []
    monitor._classify_one_and_maybe_push(
        rule=rule, tweet=tweet, profile=UserInterestProfile(chat_id="chat-A"),
        feedback_for_rule={}, footer_counts={},
        force_bypass_keyword="アビスアイ box", delivered=delivered,
    )

    assert delivered == ["t1"]
    assert len(msgs) == 1
    assert "命中你設的篩選關鍵字「アビスアイ box」" in msgs[0]
    record = monitor._db.recorded[0]
    assert record["bypass_reason"] == "explicit_keyword"

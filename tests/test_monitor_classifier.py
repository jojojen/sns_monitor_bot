"""Guardrail tests for SnsMonitor's classifier wiring.

These are the regression tests that protect the user's existing useful
features (explicit keyword filter + marketplace watchlist) — see the plan's
'Bypass A' / 'Bypass B' guardrails section.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

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
    monitor._knowledge_appender = None
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


def test_llm_giveaway_spam_verdict_overrides_keyword_bypass():
    """User set include_keywords=('抽選',) and a follow+retweet raffle mentions
    '抽選'. Bypass A would normally force a push — but when the LLM judges the
    post a worthless follow+retweet giveaway (giveaway_spam=true), that verdict
    overrides Bypass A: nothing is delivered and bypass_reason='giveaway_spam'
    is recorded for observability."""
    def giveaway_llm(_prompt):
        return (
            '{"long_term_score": 0, "arbitrage_score": 0, '
            '"giveaway_spam": true, "rationale": "フォロー+リポスト無償抽選"}'
        )

    delivered_messages: list[tuple[str, str]] = []
    monitor = _make_monitor(llm_fn=giveaway_llm)
    monitor._notify_fn = lambda chat_id, text, reply_markup=None: delivered_messages.append((chat_id, text))

    rule = _make_account_rule(include_keywords=("抽選",))
    tweet = _make_tweet(
        "本日のプレゼント企画\n参加条件\n① @shop をフォロー\n② リポスト\n完売後に抽選"
    )

    from sns_monitor.interest_profile import UserInterestProfile
    profile = UserInterestProfile(chat_id="chat-A")
    delivered: list[str] = []
    monitor._classify_one_and_maybe_push(
        rule=rule, tweet=tweet, profile=profile,
        feedback_for_rule={}, footer_counts={},
        force_bypass_keyword=None,
        delivered=delivered,
    )

    assert delivered == [], "follow+retweet giveaway must not be pushed"
    assert delivered_messages == []
    record = monitor._db.recorded[0]
    assert record["bypass_reason"] == "giveaway_spam"
    assert record["pushed"] == 0


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
        return (
            '{"long_term_score": 85, "arbitrage_score": 20, '
            '"suggested_action": "加入長期 watchlist", "actionability": "concrete", '
            '"purchase_target": {"sku_or_title": "アビスアイ box", '
            '"purchase_url": "https://mercari.jp/items/x"}}'
        )

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
        return (
            '{"long_term_score": 80, "arbitrage_score": 80, '
            '"actionability": "concrete", '
            '"purchase_target": {"sku_or_title": "BOX", '
            '"purchase_url": "https://mercari.jp/items/y"}}'
        )

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


# ── Actionability silence → knowledge_appender sink ─────────────────────────


def test_silenced_vague_signal_skips_notify_and_calls_appender():
    """A long-term signal without a buy-now surface (actionability=vague,
    no Bypass A) must NOT push to Telegram. Instead, it gets sunk into the
    lobster knowledge base — once per matched entity."""
    def vague_llm(_prompt):
        return (
            '{"long_term_score": 85, "arbitrage_score": 30, '
            '"suggested_action": "關注發售資訊", '
            '"rationale": "新弾発表，前置訊號", '
            '"actionability": "vague"}'
        )

    monitor = _make_monitor(llm_fn=vague_llm)
    # Force matched_entities into the classifier output via the alias source.
    class _Aliases:
        def all_aliases(self):
            return [("UNION ARENA", "union_arena")]
        def lookup_canonical(self, alias):
            return "union_arena" if alias.lower() == "union arena" else None
    monitor._alias_source = _Aliases()

    appended: list[dict] = []
    monitor._knowledge_appender = lambda payload: appended.append(payload)

    notify_calls: list = []
    monitor._notify_fn = lambda *a, **kw: notify_calls.append(a)

    rule = _make_account_rule(include_keywords=())
    tweet = _make_tweet("UNION ARENA 新弾発売決定！")

    from sns_monitor.interest_profile import UserInterestProfile
    delivered: list[str] = []
    monitor._classify_one_and_maybe_push(
        rule=rule, tweet=tweet, profile=UserInterestProfile(chat_id="chat-A"),
        feedback_for_rule={}, footer_counts={},
        force_bypass_keyword=None, delivered=delivered,
    )

    assert delivered == [], "vague signal must not push"
    assert notify_calls == [], "notify_fn must not be called for vague signal"
    record = monitor._db.recorded[0]
    assert record["pushed"] == 0
    assert record["bypass_reason"] == "none"
    assert len(appended) == 1, f"expected 1 appender call per matched entity, got {appended}"
    payload = appended[0]
    assert payload["entity"] == "union_arena"
    assert payload["rationale"] == "新弾発表，前置訊號"
    assert payload["suggested_action"] == "關注發售資訊"
    assert payload["tweet_url"] == "https://x.com/alice/status/t1"


def test_bypass_a_still_pushes_when_vague_and_skips_appender():
    """Bypass A wins over the actionability gate — user-set keyword filter
    is the authority. The silenced sink should NOT run on a pushed signal."""
    def vague_llm(_prompt):
        return (
            '{"long_term_score": 5, "arbitrage_score": 5, '
            '"actionability": "vague"}'
        )

    monitor = _make_monitor(llm_fn=vague_llm)
    appended: list[dict] = []
    monitor._knowledge_appender = lambda payload: appended.append(payload)
    delivered_messages: list[tuple[str, str]] = []
    monitor._notify_fn = lambda chat_id, text, reply_markup=None: delivered_messages.append((chat_id, text))

    rule = _make_account_rule(include_keywords=("抽選",))
    tweet = _make_tweet("これは抽選の話")

    from sns_monitor.interest_profile import UserInterestProfile
    delivered: list[str] = []
    monitor._classify_one_and_maybe_push(
        rule=rule, tweet=tweet, profile=UserInterestProfile(chat_id="chat-A"),
        feedback_for_rule={}, footer_counts={},
        force_bypass_keyword=None, delivered=delivered,
    )

    assert delivered == ["t1"], "Bypass A must push even on vague signal"
    assert len(delivered_messages) == 1
    assert appended == [], "appender must not run on pushed signal"
    record = monitor._db.recorded[0]
    assert record["pushed"] == 1
    assert record["bypass_reason"] == "explicit_keyword"


def test_event_signal_end_to_end_pushes_with_event_headline_and_schedules_reminders(tmp_path):
    """Real DB integration: event push uses the event headline, persists two
    due reminders, then due-check delivery sends them and marks them sent."""
    import asyncio

    from sns_monitor.interest_profile import UserInterestProfile
    from sns_monitor.models import utc_now
    from sns_monitor.storage import SnsDatabase

    now = utc_now()
    deadline = (now + timedelta(hours=2)).replace(microsecond=0).isoformat()
    event_date = (now + timedelta(hours=3)).replace(microsecond=0).isoformat()

    def llm(_prompt):
        return (
            '{"long_term_score": 0, "arbitrage_score": 0, '
            '"actionability": "vague", '
            '"is_event_signal": true, '
            '"event_name": "Project SEKAI x Lawson", '
            f'"event_date": "{event_date}", '
            '"event_location": "日本", '
            '"signup_url": "https://lawson.example/apply", '
            '"recommended_character": "巡音ルカ", '
            f'"deadline": "{deadline}", '
            '"rationale": "限定聯名公告"}'
        )

    sent: list[tuple[str, str]] = []

    def notify(chat_id, text, *args):
        sent.append((chat_id, text))

    db_path = tmp_path / "sns.sqlite3"
    SnsDatabase(db_path).bootstrap()
    monitor = SnsMonitor(
        db_path=str(db_path),
        notify_fn=notify,
        sources={},
        classifier_llm_fn=llm,
        alias_source=_StubAliasSource(),
        min_score_to_push=60,
    )
    rule = _make_account_rule()
    tweet = _make_tweet("Project SEKAI x Lawson 限定アクスタ 抽選受付開始")

    monitor._classify_one_and_maybe_push(
        rule=rule,
        tweet=tweet,
        profile=UserInterestProfile(chat_id="chat-A"),
        feedback_for_rule={},
        footer_counts={},
        force_bypass_keyword=None,
        delivered=[],
    )

    assert len(sent) == 1
    initial_text = sent[0][1]
    assert "📅 限定活動訊號" in initial_text
    assert "活動：Project SEKAI x Lawson" in initial_text
    assert "🎯 推薦角色：巡音ルカ" in initial_text

    db = SnsDatabase(db_path)
    due = db.list_due_reminders(utc_now().isoformat())
    kinds = {row["kind"] for row in due}
    assert kinds == {"signup", "event"}
    for row in due:
        assert "📅 限定活動訊號" in str(row["payload_text"])

    asyncio.run(monitor._check_due_reminders())

    assert len(sent) == 3
    reminder_texts = [text for _chat_id, text in sent[1:]]
    assert any(text.startswith("⏰ 報名前提醒\n\n📅 限定活動訊號") for text in reminder_texts)
    assert any(text.startswith("⏰ 活動前提醒\n\n📅 限定活動訊號") for text in reminder_texts)
    assert db.list_due_reminders(utc_now().isoformat()) == []

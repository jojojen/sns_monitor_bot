"""Layer C: structured collectible-signal extraction + no-sentiment summary."""

from __future__ import annotations

from datetime import datetime, timezone

from sns_monitor.digest import (
    BuzzTarget,
    BuzzResult,
    _build_prompt,
    _compose_summary,
    _coerce_optional,
    _parse_llm_response,
    summarize_topic_sync,
)
from sns_monitor.models import Tweet


def _tweet(i: int) -> Tweet:
    return Tweet(
        tweet_id=str(i),
        author_handle="/vp/",
        author_id="",
        text=f"thread {i}",
        created_at=datetime.now(timezone.utc),
        like_count=i,
    )


def _tweets(n: int) -> list[Tweet]:
    return [_tweet(i) for i in range(1, n + 1)]


# ── has_signal gating ───────────────────────────────────────────────────────


def test_has_signal_true_with_catalyst():
    r = BuzzResult(query="q", summary="", sources=[], fetched_count=0, catalyst="新彈上市")
    assert r.has_signal


def test_has_signal_true_with_hot_items():
    r = BuzzResult(query="q", summary="", sources=[], fetched_count=0,
                   hot_items=("TCG Pocket",))
    assert r.has_signal


def test_has_signal_true_with_high_signal():
    r = BuzzResult(query="q", summary="", sources=[], fetched_count=0,
                   collectible_signal="high")
    assert r.has_signal


def test_has_signal_false_for_bare_chatter():
    r = BuzzResult(query="q", summary="", sources=[], fetched_count=0,
                   collectible_signal="low")
    assert not r.has_signal


# ── _coerce_optional ────────────────────────────────────────────────────────


def test_coerce_optional_handles_null_and_none():
    assert _coerce_optional(None) == ""
    assert _coerce_optional("null") == ""
    assert _coerce_optional("  ") == ""
    assert _coerce_optional(" 新彈 ") == "新彈"


# ── _compose_summary (noun-driven, no sentiment) ────────────────────────────


def test_compose_summary_empty_is_neutral_placeholder():
    out = _compose_summary((), "", "", "low")
    assert "一般討論" in out
    assert "低" in out


def test_compose_summary_includes_nouns_and_catalyst():
    out = _compose_summary(("リコリス アクスタ",), "復刻", "限定盒", "high")
    assert "リコリス アクスタ" in out
    assert "復刻" in out
    assert "限定盒" in out
    assert "高" in out


def test_compose_summary_groups_categorized_targets():
    out = _compose_summary(
        (),
        "限定ガチャ",
        "",
        "medium",
        (
            BuzzTarget(category="gacha", name="Past Fragments"),
            BuzzTarget(category="character", name="巡音ルカ"),
        ),
    )
    assert "卡池：Past Fragments" in out
    assert "角色：巡音ルカ" in out
    assert "限定ガチャ" in out


# ── _parse_llm_response ─────────────────────────────────────────────────────


def test_parse_extracts_structured_fields():
    tweets = _tweets(5)
    raw = (
        '{"hot_items": ["TCG Pocket", "Champions"], "catalyst": "新彈上市", '
        '"actionable": "關注未開封盒", "collectible_signal": "high", "picks": [1, 3]}'
    )
    parsed = _parse_llm_response(raw, tweets)
    assert parsed.hot_items == ("TCG Pocket", "Champions")
    assert parsed.catalyst == "新彈上市"
    assert parsed.actionable == "關注未開封盒"
    assert parsed.collectible_signal == "high"
    assert [t.tweet_id for t in parsed.picks] == ["1", "3"]


def test_parse_extracts_categorized_targets():
    tweets = _tweets(3)
    raw = (
        '{"hot_items": [], "catalyst": "World Link", "collectible_signal": "medium", '
        '"targets": ['
        '{"category": "gacha", "name": "Past Fragments", "reason": "limited", '
        '"evidence": "Current Gacha Past Fragments", "confidence": 88}, '
        '{"category": "character", "name": "巡音ルカ", "confidence": 90}'
        '], "picks": [1]}'
    )
    parsed = _parse_llm_response(raw, tweets)
    assert [t.name for t in parsed.targets] == ["Past Fragments", "巡音ルカ"]
    assert parsed.targets[0].category == "gacha"
    assert parsed.targets[0].confidence == 88


def test_parse_strips_markdown_fence():
    tweets = _tweets(3)
    raw = '```json\n{"hot_items": [], "catalyst": null, "collectible_signal": "low"}\n```'
    parsed = _parse_llm_response(raw, tweets)
    assert parsed.hot_items == ()
    assert parsed.catalyst == ""
    assert parsed.collectible_signal == "low"


def test_parse_malformed_falls_back_to_no_signal():
    tweets = _tweets(3)
    parsed = _parse_llm_response("not json at all", tweets)
    assert parsed.hot_items == ()
    assert parsed.catalyst == ""
    assert parsed.collectible_signal == "low"
    # graceful: first few tweets become picks so the reply still has sources
    assert parsed.picks == tweets[:3]


def test_parse_invalid_signal_defaults_low():
    tweets = _tweets(3)
    raw = '{"collectible_signal": "ultra", "picks": []}'
    parsed = _parse_llm_response(raw, tweets)
    assert parsed.collectible_signal == "low"


def test_parse_drops_null_strings_in_hot_items():
    tweets = _tweets(3)
    raw = '{"hot_items": ["real", "null", "  "], "collectible_signal": "medium"}'
    parsed = _parse_llm_response(raw, tweets)
    assert parsed.hot_items == ("real",)


# ── _build_prompt specificity + deep_context ────────────────────────────────


def test_prompt_forbids_board_general_names():
    prompt = _build_prompt("pjsk", _tweets(2))
    assert "嚴禁輸出看板名" in prompt
    assert "General" in prompt


def test_prompt_includes_deep_context_when_present():
    prompt = _build_prompt("pjsk", _tweets(2), deep_context="DEEP-CTX-MARKER")
    assert "DEEP-CTX-MARKER" in prompt
    assert "【最熱串的實際討論內容" in prompt


def test_prompt_includes_entity_context_when_present():
    prompt = _build_prompt("pjsk", _tweets(2), entity_context="IP: Project SEKAI\ncharacter: 巡音ルカ")
    assert "【可擴充 IP 辭典" in prompt
    assert "Project SEKAI" in prompt
    assert "targets" in prompt


def test_prompt_omits_deep_section_when_absent():
    prompt = _build_prompt("pjsk", _tweets(2))
    assert "【最熱串的實際討論內容" not in prompt


# ── injection: llm_call_fn + deep_context_fn ───────────────────────────────


class _FakeX:
    def __init__(self, tweets):
        self._tweets = tweets

    async def search(self, query, *, count=15, aliases=()):
        return self._tweets


def test_summarize_uses_injected_llm_and_deep_context():
    captured = {}

    def fake_llm(prompt):
        captured["prompt"] = prompt
        return (
            '{"hot_items": ["Leo/need", "25-ji 限定アクスタ"], "catalyst": "新活動ガチャ", '
            '"actionable": "限定アクスタ 復刻可能", "collectible_signal": "high", "picks": [1]}'
        )

    def fake_deep(tweets):
        return "DEEP-XYZ: Leo/need 限定アクスタ restock"

    res = summarize_topic_sync(
        "pjsk",
        x_client=_FakeX(_tweets(3)),
        llm_endpoint="http://x",
        llm_model="m",
        llm_call_fn=fake_llm,
        deep_context_fn=fake_deep,
    )

    # deep context reached the prompt; injected llm result was parsed.
    assert "DEEP-XYZ" in captured["prompt"]
    assert res is not None
    assert res.hot_items == ("Leo/need", "25-ji 限定アクスタ")
    assert res.collectible_signal == "high"
    assert res.has_signal
    assert "Leo/need" in res.summary


def test_summarize_uses_entity_context_fn():
    captured = {}

    def fake_llm(prompt):
        captured["prompt"] = prompt
        return (
            '{"hot_items": [], "catalyst": "限定ガチャ", "collectible_signal": "medium", '
            '"targets": [{"category": "gacha", "name": "Past Fragments", "confidence": 80}], '
            '"picks": [1]}'
        )

    def fake_entities(query, aliases, tweets, deep_context):
        assert query == "pjsk"
        assert aliases == ("Project Sekai",)
        return "IP: Project SEKAI\ncharacter: 巡音ルカ"

    res = summarize_topic_sync(
        "pjsk",
        x_client=_FakeX(_tweets(2)),
        llm_endpoint="http://x",
        llm_model="m",
        llm_call_fn=fake_llm,
        entity_context_fn=fake_entities,
        search_aliases=("Project Sekai",),
    )

    assert "IP: Project SEKAI" in captured["prompt"]
    assert res is not None
    assert res.targets[0].name == "Past Fragments"
    assert "卡池：Past Fragments" in res.summary

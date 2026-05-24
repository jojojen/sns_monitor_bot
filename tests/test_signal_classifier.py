"""Unit tests for the two-opportunity SNS signal classifier.

Focus areas:
  - LLM JSON parsing tolerates fences / wrapper text
  - Score clamping rejects nonsense
  - Empty-input / LLM-failure fallback returns zero-score (conservative)
  - decide_push_reason honors Bypass A (explicit keyword)
"""

from __future__ import annotations

from sns_monitor.signal_classifier import (
    DEFAULT_MIN_SCORE_TO_PUSH,
    SnsPostSignal,
    _clamp_score,
    _coerce_str_list,
    _parse_classifier_response,
    build_classifier_prompt,
    classify_sns_signal,
    decide_push_reason,
)


def _make_kwargs(**overrides):
    base = dict(
        tweet_id="t1", rule_id="r1",
        author_handle="user", created_at="2026-05-23 10:00 UTC",
        tweet_text="アビスアイ box 抽選販売スタート",
        watchlist_queries=("アビスアイ box",),
        pinned_targets=(),
        feedback_for_rule={"up": 1, "down": 0, "bought": 0},
        knowledge_block="(無)",
        matched_entities=(),
        llm_fn=None,
    )
    base.update(overrides)
    return base


# ── prompt rendering ────────────────────────────────────────────────────────


def test_build_classifier_prompt_includes_user_signals_and_knowledge():
    prompt = build_classifier_prompt(
        tweet_id="t1", author_handle="x", created_at="2026-05-23",
        tweet_text="アビスアイ box 抽選",
        watchlist_queries=("アビスアイ box", "水野愛 ssp"),
        pinned_targets=("ピカチュウex SAR",),
        feedback_for_rule={"up": 5, "bought": 2, "down": 1},
        knowledge_block="- pjsk (ip): プロセカ…",
    )
    assert "アビスアイ box" in prompt
    assert "水野愛 ssp" in prompt
    assert "ピカチュウex SAR" in prompt
    assert "👍 過此 rule 的次數：5" in prompt
    assert "💰 因此 rule 下手過：2" in prompt
    assert "👎 過此 rule 的次數：1" in prompt
    assert "pjsk (ip)" in prompt
    assert "long_term_score" in prompt
    assert "arbitrage_score" in prompt


def test_build_classifier_prompt_uses_placeholder_for_empty_lists():
    prompt = build_classifier_prompt(
        tweet_id="t", author_handle="x", created_at="2026",
        tweet_text="hi", watchlist_queries=(), pinned_targets=(),
        feedback_for_rule={}, knowledge_block="(無)",
    )
    assert "（無）" in prompt  # used twice for empty watchlist/pinned


# ── parser & coercion ──────────────────────────────────────────────────────


def test_parse_classifier_response_strips_markdown_fences():
    raw = "```json\n{\"long_term_score\": 80, \"arbitrage_score\": 20}\n```"
    parsed = _parse_classifier_response(raw)
    assert parsed == {"long_term_score": 80, "arbitrage_score": 20}


def test_parse_classifier_response_finds_json_object_in_chatter():
    raw = "好喔，這是分析結果：\n{\"long_term_score\": 50, \"arbitrage_score\": 70}\n以上。"
    parsed = _parse_classifier_response(raw)
    assert parsed["arbitrage_score"] == 70


def test_parse_classifier_response_returns_none_for_garbage():
    assert _parse_classifier_response("") is None
    assert _parse_classifier_response("not json at all") is None


def test_clamp_score_handles_garbage():
    assert _clamp_score(150) == 100
    assert _clamp_score(-10) == 0
    assert _clamp_score("abc") == 0
    assert _clamp_score(None) == 0
    assert _clamp_score(75) == 75


def test_coerce_str_list_dedup_case_insensitive_with_limit():
    out = _coerce_str_list(
        ["a", "A", "b", "B", "c", "d", "e", "f", "g", "h", "i"],
        limit=5,
    )
    assert out == ("a", "b", "c", "d", "e")


def test_coerce_str_list_drops_non_strings_and_blanks():
    out = _coerce_str_list(["valid", 5, None, "", "  ", "another"])
    assert out == ("valid", "another")


# ── classify_sns_signal end-to-end ──────────────────────────────────────────


def test_classify_returns_zero_when_llm_fn_is_none():
    signal = classify_sns_signal(**_make_kwargs(llm_fn=None))
    assert signal.long_term_score == 0
    assert signal.arbitrage_score == 0
    assert signal.rationale == "(分類失敗)"


def test_classify_returns_zero_when_llm_raises():
    def bad_llm(_prompt):
        raise RuntimeError("ollama unreachable")
    signal = classify_sns_signal(**_make_kwargs(llm_fn=bad_llm))
    assert signal.long_term_score == 0
    assert signal.arbitrage_score == 0


def test_classify_returns_zero_when_llm_returns_garbage():
    def garbage_llm(_prompt):
        return "lol not json"
    signal = classify_sns_signal(**_make_kwargs(llm_fn=garbage_llm))
    assert signal.long_term_score == 0


def test_classify_parses_strong_signal_from_llm():
    def good_llm(_prompt):
        return (
            '{"long_term_score": 85, "arbitrage_score": 75, '
            '"matched_products": ["アビスアイ box"], '
            '"matched_keywords": ["抽選"], '
            '"suggested_action": "立即查 Mercari 是否有低於 ¥10500 listing", '
            '"rationale": "限定抽選 + watchlist 命中", '
            '"deadline": "2026-05-26T23:59:00+09:00"}'
        )
    signal = classify_sns_signal(**_make_kwargs(llm_fn=good_llm))
    assert signal.long_term_score == 85
    assert signal.arbitrage_score == 75
    assert signal.matched_products == ("アビスアイ box",)
    assert signal.matched_keywords == ("抽選",)
    assert signal.deadline_iso == "2026-05-26T23:59:00+09:00"
    assert "Mercari" in signal.suggested_action


def test_classify_treats_null_deadline_as_none():
    def llm(_prompt):
        return '{"long_term_score": 70, "arbitrage_score": 30, "deadline": null}'
    signal = classify_sns_signal(**_make_kwargs(llm_fn=llm))
    assert signal.deadline_iso is None


def test_classify_propagates_matched_entities_through():
    """matched_entities is set by caller (alias extractor), not the LLM, so it
    must come through unchanged whether LLM succeeds or fails."""
    signal = classify_sns_signal(**_make_kwargs(
        llm_fn=None, matched_entities=("pjsk", "ホロライブ"),
    ))
    assert signal.matched_entities == ("pjsk", "ホロライブ")


# ── decide_push_reason (Bypass A guardrail) ─────────────────────────────────


def _signal(lt=0, arb=0):
    return SnsPostSignal(
        tweet_id="t", rule_id="r",
        long_term_score=lt, arbitrage_score=arb,
        matched_products=(), matched_keywords=(), matched_entities=(),
        suggested_action="", rationale="", deadline_iso=None,
    )


def test_explicit_keyword_bypass_always_wins_even_at_zero_score():
    """The guardrail: a rule whose include_keywords matched a tweet must push
    regardless of LLM verdict. This protects the user's hand-set filters."""
    reason = decide_push_reason(signal=_signal(lt=0, arb=0), keyword_matched=True)
    assert reason == "explicit_keyword"


def test_decide_push_reason_both_high():
    reason = decide_push_reason(signal=_signal(lt=80, arb=80), keyword_matched=False)
    assert reason == "both"


def test_decide_push_reason_long_term_only():
    reason = decide_push_reason(signal=_signal(lt=70, arb=40), keyword_matched=False)
    assert reason == "long_term"


def test_decide_push_reason_arbitrage_only():
    reason = decide_push_reason(signal=_signal(lt=20, arb=70), keyword_matched=False)
    assert reason == "arbitrage"


def test_decide_push_reason_drops_when_both_below_threshold():
    reason = decide_push_reason(signal=_signal(lt=30, arb=30), keyword_matched=False)
    assert reason == "none"


def test_decide_push_reason_respects_custom_threshold():
    sig = _signal(lt=40, arb=40)
    assert decide_push_reason(signal=sig, keyword_matched=False, min_score=DEFAULT_MIN_SCORE_TO_PUSH) == "none"
    assert decide_push_reason(signal=sig, keyword_matched=False, min_score=30) == "both"

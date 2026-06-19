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
    FEEDBACK_SCORE_FLOOR,
    SnsPostSignal,
    _clamp_score,
    _coerce_str_list,
    _effective_min_score,
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


def test_build_classifier_prompt_includes_ex_ante_signal_rubric():
    """A3: rubric must teach the LLM to give 60+ score for IP × TCG collab
    announcements, 予約開始, アニメ phase decisions etc., even when the product
    is not yet in user watchlist — these are the chainsaw-ua use case signals."""
    prompt = build_classifier_prompt(
        tweet_id="t", author_handle="x", created_at="2026",
        tweet_text="any",
        watchlist_queries=(), pinned_targets=(), feedback_for_rule={},
        knowledge_block="(無)",
    )
    # Section header must appear
    assert "Ex-ante 事前訊號特別加權" in prompt
    # Specific ex-ante signal types must be enumerated
    assert "IP × TCG collab 公告" in prompt
    assert "予約開始" in prompt
    assert "抽選販賣公告" in prompt
    assert "アニメ第 N 期発表" in prompt
    assert "新弾発売決定" in prompt
    # Must explicitly state the score floor for ex-ante signals even when
    # the product is NOT in user watchlist — this is the key behaviour
    # the chainsaw-ua use case depends on.
    assert "60+" in prompt or "60+ 分" in prompt


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


def test_parse_classifier_response_strips_think_tags():
    """qwen3-style <think>…</think> blocks must be stripped before JSON parsing."""
    raw = "<think>Let me analyze this tweet carefully...</think>\n{\"long_term_score\": 75, \"is_event_signal\": true}"
    parsed = _parse_classifier_response(raw)
    assert parsed is not None
    assert parsed["is_event_signal"] is True
    assert parsed["long_term_score"] == 75


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


def test_classify_parses_event_fields_from_llm():
    def llm(_prompt):
        return (
            '{"long_term_score": 40, "arbitrage_score": 20, '
            '"is_event_signal": true, '
            '"event_name": "名探偵プリキュア！展", '
            '"event_date": "2026-05-15", '
            '"event_location": "横浜・新高島駅B1F Art Center NEW", '
            '"signup_url": "https://w.pia.jp/t/precure-exh/", '
            '"recommended_character": "会場限定グッズ（キュアアルカナ）"}'
        )
    signal = classify_sns_signal(**_make_kwargs(llm_fn=llm))
    assert signal.is_event_signal is True
    assert signal.event_name == "名探偵プリキュア！展"
    assert signal.event_date == "2026-05-15"
    assert "Art Center" in signal.event_location
    assert signal.signup_url == "https://w.pia.jp/t/precure-exh/"
    assert "キュアアルカナ" in signal.recommended_character


def test_classify_event_fields_default_when_missing():
    def llm(_prompt):
        return '{"long_term_score": 10, "arbitrage_score": 10}'
    signal = classify_sns_signal(**_make_kwargs(llm_fn=llm))
    assert signal.is_event_signal is False
    assert signal.event_name == ""
    assert signal.event_date == ""
    assert signal.event_location == ""
    assert signal.signup_url == ""
    assert signal.recommended_character == ""


def test_classify_event_fields_treat_null_string_as_empty():
    def llm(_prompt):
        return (
            '{"long_term_score": 10, "arbitrage_score": 10, '
            '"is_event_signal": true, "recommended_character": "null", '
            '"event_name": "   "}'
        )
    signal = classify_sns_signal(**_make_kwargs(llm_fn=llm))
    assert signal.recommended_character == ""
    assert signal.event_name == ""


def test_classify_propagates_matched_entities_through():
    """matched_entities is set by caller (alias extractor), not the LLM, so it
    must come through unchanged whether LLM succeeds or fails."""
    signal = classify_sns_signal(**_make_kwargs(
        llm_fn=None, matched_entities=("pjsk", "ホロライブ"),
    ))
    assert signal.matched_entities == ("pjsk", "ホロライブ")


# ── decide_push_reason (Bypass A guardrail) ─────────────────────────────────


def _signal(lt=0, arb=0, actionability="concrete", purchase_target_json=None,
            giveaway_spam=False, is_event_signal=False):
    """Helper for score-gate tests. Defaults to actionability='concrete' so
    existing tests exercise the score logic (not the actionability gate).
    The dedicated actionability tests below pass actionability='vague'
    explicitly."""
    return SnsPostSignal(
        tweet_id="t", rule_id="r",
        long_term_score=lt, arbitrage_score=arb,
        matched_products=(), matched_keywords=(), matched_entities=(),
        suggested_action="", rationale="", deadline_iso=None,
        actionability=actionability,
        purchase_target_json=purchase_target_json,
        giveaway_spam=giveaway_spam,
        is_event_signal=is_event_signal,
    )


def test_decide_push_reason_giveaway_spam_overrides_keyword_bypass():
    """LLM giveaway_spam verdict is the highest-priority gate: a worthless
    follow+retweet raffle never pushes, even when the rule's include_keyword
    matched (Bypass A)."""
    reason = decide_push_reason(
        signal=_signal(lt=0, arb=0, giveaway_spam=True), keyword_matched=True
    )
    assert reason == "giveaway_spam"


def test_decide_push_reason_legit_lottery_still_bypasses_on_keyword():
    """A legit 抽選販売 (giveaway_spam=False) that matched the keyword must
    still push via Bypass A — the override only fires on the LLM verdict."""
    reason = decide_push_reason(
        signal=_signal(lt=0, arb=0, giveaway_spam=False), keyword_matched=True
    )
    assert reason == "explicit_keyword"


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


# ── event-signal gate (announce-stage relaxation) ───────────────────────────


def test_event_signal_pushes_even_when_vague():
    """A limited IP-collab event announcement with no buy link yet
    (actionability='vague') must still push — it bypasses the actionability
    gate and falls through to the 'event' reason when no score fires."""
    sig = _signal(lt=0, arb=0, actionability="vague", is_event_signal=True)
    assert decide_push_reason(signal=sig, keyword_matched=False) == "event"


def test_event_signal_with_score_uses_score_reason():
    """When an event signal also clears the score gate, the richer score
    reason wins over the bare 'event' fallthrough."""
    sig = _signal(lt=80, arb=20, actionability="vague", is_event_signal=True)
    assert decide_push_reason(signal=sig, keyword_matched=False) == "long_term"


def test_giveaway_spam_still_overrides_event_signal():
    """giveaway_spam stays the highest-priority gate even if the LLM also
    marked is_event_signal — a worthless raffle never pushes."""
    sig = _signal(lt=0, arb=0, giveaway_spam=True, is_event_signal=True)
    assert decide_push_reason(signal=sig, keyword_matched=True) == "giveaway_spam"


def test_non_event_vague_signal_still_silenced():
    """The relaxation is scoped to event signals only: an ordinary vague
    signal with no event flag is still dropped."""
    sig = _signal(lt=0, arb=0, actionability="vague", is_event_signal=False)
    assert decide_push_reason(signal=sig, keyword_matched=False) == "none"


# ── feedback push-probability boost ─────────────────────────────────────────


def test_effective_min_score_no_feedback_is_unchanged():
    assert _effective_min_score(60, None) == 60
    assert _effective_min_score(60, {}) == 60


def test_effective_min_score_bought_lowers_threshold():
    assert _effective_min_score(60, {"bought": 1}) == 55
    assert _effective_min_score(60, {"bought": 2}) == 50


def test_effective_min_score_up_lowers_threshold():
    assert _effective_min_score(60, {"up": 3}) == 54


def test_effective_min_score_floored_at_50():
    # Large positive feedback can't push the gate below the floor (50).
    assert _effective_min_score(60, {"up": 5, "bought": 2}) == FEEDBACK_SCORE_FLOOR
    assert _effective_min_score(60, {"bought": 10}) == FEEDBACK_SCORE_FLOOR


def test_effective_min_score_ignores_down():
    assert _effective_min_score(60, {"down": 9}) == 60


def test_decide_push_reason_feedback_surfaces_borderline_tweet():
    # Score 52 is below the default 60 gate → dropped without feedback…
    sig = _signal(lt=52, arb=0)
    assert decide_push_reason(signal=sig, keyword_matched=False) == "none"
    # …but two 💰 lower the gate to 50, so it now pushes.
    assert decide_push_reason(
        signal=sig, keyword_matched=False, feedback_for_rule={"bought": 2},
    ) == "long_term"


def test_decide_push_reason_feedback_never_surfaces_below_floor():
    # Score 45 stays below the 50 floor regardless of feedback magnitude.
    sig = _signal(lt=45, arb=45)
    assert decide_push_reason(
        signal=sig, keyword_matched=False, feedback_for_rule={"bought": 10},
    ) == "none"


def test_decide_push_reason_feedback_does_not_override_actionability():
    # Vague signals never push, even with heavy positive feedback.
    sig = _signal(lt=90, arb=90, actionability="vague")
    assert decide_push_reason(
        signal=sig, keyword_matched=False, feedback_for_rule={"bought": 5},
    ) == "none"


# ── heat_block injection ─────────────────────────────────────────────────────


def test_build_classifier_prompt_includes_heat_block_when_provided():
    prompt = build_classifier_prompt(
        tweet_id="t1", author_handle="x", created_at="2026-05-25",
        tweet_text="チェンソーマン UA 発表",
        watchlist_queries=(), pinned_targets=(),
        feedback_for_rule={}, knowledge_block="(無)",
        heat_block="- チェンソーマン: x_mention percentile=87, google_trends percentile=92",
    )
    assert "IP 熱度指標" in prompt
    assert "percentile=87" in prompt
    assert "percentile=92" in prompt


def test_build_classifier_prompt_no_heat_block_section_when_empty():
    prompt = build_classifier_prompt(
        tweet_id="t1", author_handle="x", created_at="2026-05-25",
        tweet_text="test tweet",
        watchlist_queries=(), pinned_targets=(),
        feedback_for_rule={}, knowledge_block="(無)",
        heat_block="",
    )
    assert "IP 熱度指標" not in prompt


def test_classify_sns_signal_passes_heat_block_to_prompt():
    captured: list[str] = []

    def fake_llm(prompt: str) -> str:
        captured.append(prompt)
        return '{"long_term_score":70,"arbitrage_score":50,"matched_products":[],"matched_keywords":[],"suggested_action":"test","rationale":"test","deadline":null}'

    kwargs = _make_kwargs(
        llm_fn=fake_llm,
        heat_block="- チェンソーマン: x_mention percentile=90",
    )
    classify_sns_signal(**kwargs)
    assert captured, "LLM was not called"
    assert "percentile=90" in captured[0]


# ── actionability gate (silence vague non-keyword signals) ─────────────────


def test_prompt_contains_actionability_contract():
    prompt = build_classifier_prompt(
        tweet_id="t1", author_handle="x", created_at="2026-05-27",
        tweet_text="UNION ARENA 新弾発表",
        watchlist_queries=(), pinned_targets=(),
        feedback_for_rule={}, knowledge_block="(無)",
    )
    assert '"actionability"' in prompt
    assert '"purchase_target"' in prompt
    assert "concrete" in prompt and "vague" in prompt


def test_parser_defaults_missing_actionability_to_vague():
    def llm(_prompt):
        return '{"long_term_score": 80, "arbitrage_score": 20, "deadline": null}'
    signal = classify_sns_signal(**_make_kwargs(llm_fn=llm))
    assert signal.actionability == "vague"
    assert signal.purchase_target_json is None


def test_parser_accepts_concrete_with_valid_purchase_target():
    def llm(_prompt):
        return (
            '{"long_term_score": 80, "arbitrage_score": 80, "deadline": null, '
            '"actionability": "concrete", '
            '"purchase_target": {"sku_or_title": "UA20BT BOX", '
            '"purchase_url": "https://mercari.jp/items/m12345", '
            '"price_hint": "¥5,500"}}'
        )
    signal = classify_sns_signal(**_make_kwargs(llm_fn=llm))
    assert signal.actionability == "concrete"
    import json as _j
    pt = _j.loads(signal.purchase_target_json or "{}")
    assert pt["purchase_url"].startswith("https://mercari.jp/")
    assert pt["sku_or_title"] == "UA20BT BOX"


def test_parser_rejects_purchase_target_when_vague():
    def llm(_prompt):
        return (
            '{"long_term_score": 80, "arbitrage_score": 80, "deadline": null, '
            '"actionability": "vague", '
            '"purchase_target": {"purchase_url": "https://example.com"}}'
        )
    signal = classify_sns_signal(**_make_kwargs(llm_fn=llm))
    assert signal.actionability == "vague"
    assert signal.purchase_target_json is None


def test_parser_rejects_purchase_target_without_http_url():
    def llm(_prompt):
        return (
            '{"long_term_score": 80, "arbitrage_score": 80, "deadline": null, '
            '"actionability": "concrete", '
            '"purchase_target": {"sku_or_title": "x", "purchase_url": "not-a-url"}}'
        )
    signal = classify_sns_signal(**_make_kwargs(llm_fn=llm))
    assert signal.actionability == "concrete"
    assert signal.purchase_target_json is None


def test_decide_push_reason_silences_vague_non_keyword():
    sig = _signal(lt=90, arb=20, actionability="vague")
    assert decide_push_reason(signal=sig, keyword_matched=False) == "none"


def test_decide_push_reason_keyword_bypass_overrides_vague():
    sig = _signal(lt=10, arb=10, actionability="vague")
    assert decide_push_reason(signal=sig, keyword_matched=True) == "explicit_keyword"


def test_decide_push_reason_concrete_still_needs_min_score():
    sig = _signal(lt=10, arb=10, actionability="concrete")
    assert decide_push_reason(signal=sig, keyword_matched=False) == "none"

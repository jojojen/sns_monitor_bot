"""Unit tests for entity_extractor — alias substring + LLM fallback."""

from __future__ import annotations

from sns_monitor.entity_extractor import extract_entities


class _StubAliasSource:
    def __init__(self, aliases: list[tuple[str, str]]) -> None:
        self._aliases = aliases

    def all_aliases(self) -> list[tuple[str, str]]:
        return self._aliases

    def lookup_canonical(self, alias: str) -> str | None:
        lower = alias.casefold()
        for a, canonical in self._aliases:
            if a.casefold() == lower:
                return canonical
        return None


def test_alias_substring_match_skips_llm_when_hit():
    alias_source = _StubAliasSource([("PJSK", "pjsk"), ("ホロライブ", "hololive")])
    llm_called = []

    def llm(_prompt):
        llm_called.append(True)
        return '{"entities": ["IGNORED"]}'

    known, novel = extract_entities(
        "PJSK の新弾発表！", alias_source=alias_source, llm_fn=llm,
    )
    assert known == ("pjsk",)
    assert novel == ()
    assert llm_called == [], "Step 1 hit — LLM must not be called"


def test_alias_match_is_case_insensitive_and_deduped():
    alias_source = _StubAliasSource([("pjsk", "pjsk"), ("PJSK", "pjsk")])
    known, novel = extract_entities("プロジェクトセカイ aka pjsk!", alias_source=alias_source)
    assert known == ("pjsk",)
    assert novel == ()


def test_llm_fallback_when_no_alias_matches():
    alias_source = _StubAliasSource([])  # nothing in DB yet

    def llm(_prompt):
        return '{"entities": ["新IP", "アビスアイ"]}'

    known, novel = extract_entities("謎の新弾が登場", alias_source=alias_source, llm_fn=llm)
    assert known == ()
    assert set(novel) == {"新IP", "アビスアイ"}


def test_llm_fallback_maps_to_canonical_when_alias_exists_for_extracted_entity():
    """If the LLM extracts an entity name that maps to a canonical via the
    alias table, treat it as known (not novel)."""
    alias_source = _StubAliasSource([("プロジェクトセカイ", "pjsk")])

    def llm(_prompt):
        return '{"entities": ["プロジェクトセカイ", "完全新規IP"]}'

    # Note: substring scan only matches if the alias is in the text; here it's
    # NOT in the text we pass in, so step 1 returns nothing → fallback fires.
    known, novel = extract_entities(
        "なんか新しいゲームについて", alias_source=alias_source, llm_fn=llm,
    )
    assert known == ("pjsk",)
    assert novel == ("完全新規IP",)


def test_llm_disabled_returns_only_alias_matches():
    """Passing llm_fn=None disables NER fallback (cost cap)."""
    alias_source = _StubAliasSource([])
    known, novel = extract_entities("謎の新弾", alias_source=alias_source, llm_fn=None)
    assert known == ()
    assert novel == ()


def test_empty_tweet_returns_empty():
    alias_source = _StubAliasSource([("PJSK", "pjsk")])
    assert extract_entities("", alias_source=alias_source) == ((), ())
    assert extract_entities("   ", alias_source=alias_source) == ((), ())


def test_min_alias_length_filters_noise_single_chars():
    alias_source = _StubAliasSource([("a", "anime"), ("PJSK", "pjsk")])
    # "a" alias is too short — should be skipped even though it appears in text
    known, novel = extract_entities("a great PJSK day", alias_source=alias_source)
    assert known == ("pjsk",)


def test_llm_returns_non_json_falls_back_silently():
    alias_source = _StubAliasSource([])

    def llm(_prompt):
        return "this is not json"

    known, novel = extract_entities("whatever", alias_source=alias_source, llm_fn=llm)
    assert known == () and novel == ()


def test_alias_source_failure_does_not_raise():
    class _Broken:
        def all_aliases(self):
            raise RuntimeError("DB went down")
        def lookup_canonical(self, _):
            return None

    known, novel = extract_entities("hi", alias_source=_Broken())
    assert known == () and novel == ()

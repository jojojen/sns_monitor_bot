"""Layer A: subject-precise + accent-aware 4chan thread matching."""

from __future__ import annotations

from sns_monitor.fourchan_buzz import _matches, _matches_any, _norm, _query_terms


def test_norm_strips_accents_and_lowercases():
    assert _norm("Pokémon") == "pokemon"
    assert _norm("  リコリス  ") == "リコリス"
    assert _norm(None) == ""


def test_accent_insensitive_subject_match():
    assert _matches("pokemon", sub="Pokémon TCG new set", com="")


def test_subject_phrase_match():
    assert _matches("chainsaw man", sub="Chainsaw Man general", com="")


def test_subject_all_terms_match_out_of_order():
    # Each term present in subject (not as a phrase) still counts.
    assert _matches("man chainsaw", sub="Chainsaw Man thread", com="")


def test_incidental_body_mention_does_not_count_when_subject_present():
    # A LEGO General thread mentioning pokemon in the OP body must NOT inflate
    # pokemon heat — subject is present but is about LEGO, not pokemon.
    assert not _matches(
        "pokemon", sub="/lg/ - LEGO General", com="anyone collect pokemon too?"
    )


def test_no_subject_falls_back_to_whole_phrase_in_body():
    assert _matches("pokemon", sub="", com="Pokemon fucking won again")


def test_no_subject_partial_terms_in_body_do_not_count():
    # Whole phrase required when there's no subject; scattered terms don't count.
    assert not _matches("chainsaw man", sub="", com="a man holding a chain saw")


def test_empty_query_never_matches():
    assert not _matches("", sub="anything", com="anything")


# ── alias-aware matching (RAG query-expansion) ──────────────────────────────


def test_query_terms_dedupes_case_insensitively_primary_first():
    terms = _query_terms("pjsk", ["PJSK", "Project Sekai", "  ", "pjsk"])
    assert terms == ("pjsk", "Project Sekai")


def test_matches_any_hits_via_alias_not_primary():
    # User typed 'pjsk'; the 4chan subject only says 'Project SEKAI'. The alias
    # makes it match where the bare primary term would not.
    sub = "/psg/ - Project SEKAI General"
    assert not _matches("pjsk", sub=sub, com="")
    assert _matches_any(("pjsk", "Project Sekai"), sub=sub, com="")


def test_matches_any_false_when_no_term_matches():
    assert not _matches_any(("pjsk", "Project Sekai"), sub="/lg/ - LEGO General", com="")

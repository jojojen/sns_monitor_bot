from __future__ import annotations

from sns_monitor.ip_entity_catalog import IpEntityCatalog


def _catalog() -> IpEntityCatalog:
    return IpEntityCatalog.from_dict(
        {
            "version": 1,
            "ips": [
                {
                    "ip_id": "project_sekai",
                    "canonical": "Project SEKAI",
                    "aliases": ["pjsk", "プロセカ"],
                    "entities": [
                        {"category": "character", "name": "巡音ルカ", "aliases": ["Luka"]},
                        {"category": "character", "name": "日野森雫", "aliases": ["Shizuku"]},
                        {"category": "event", "name": "World Link"},
                    ],
                },
                {
                    "ip_id": "pokemon_tcg",
                    "canonical": "Pokemon TCG",
                    "aliases": ["ptcg", "ポケカ"],
                    "entities": [
                        {"category": "card", "name": "SAR"},
                    ],
                },
            ],
        }
    )


def test_catalog_matches_multiple_ip_profiles():
    catalog = _catalog()
    assert catalog.match_profile("pjsk").canonical == "Project SEKAI"
    assert catalog.match_profile("ptcg").canonical == "Pokemon TCG"
    assert catalog.match_profile("unknown") is None


def test_catalog_context_uses_aliases_and_prioritizes_evidence_hits():
    context = _catalog().build_context(
        "pjsk",
        aliases=("Project Sekai",),
        evidence_text="Current Gacha Past Fragments Luka, Shizuku",
    )
    assert "IP: Project SEKAI" in context
    assert "character: 巡音ルカ" in context
    assert "character: 日野森雫" in context
    # Evidence hits should be shown before unmatched static dictionary rows.
    assert context.index("巡音ルカ") < context.index("World Link")

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable


_CATEGORY_ORDER = {
    "group": 0,
    "character": 1,
    "event": 2,
    "gacha": 3,
    "card": 4,
    "card_box": 5,
    "product": 6,
    "other": 7,
}


def _clean_str(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return value.strip()


def _clean_tuple(values: object) -> tuple[str, ...]:
    if not isinstance(values, list):
        return ()
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        cleaned = _clean_str(value)
        key = cleaned.casefold()
        if cleaned and key not in seen:
            seen.add(key)
            out.append(cleaned)
    return tuple(out)


@dataclass(frozen=True)
class IpEntity:
    category: str
    name: str
    aliases: tuple[str, ...] = ()
    meta: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "IpEntity | None":
        name = _clean_str(raw.get("name"))
        if not name:
            return None
        category = _clean_str(raw.get("category")).lower() or "other"
        if category not in _CATEGORY_ORDER:
            category = "other"
        meta_raw = raw.get("meta")
        meta: dict[str, str] = {}
        if isinstance(meta_raw, dict):
            for key, value in meta_raw.items():
                k = _clean_str(key)
                v = _clean_str(value)
                if k and v:
                    meta[k] = v
        return cls(
            category=category,
            name=name,
            aliases=_clean_tuple(raw.get("aliases")),
            meta=meta,
        )

    def searchable_terms(self) -> tuple[str, ...]:
        return (self.name, *self.aliases)

    def format_line(self) -> str:
        bits = [f"{self.category}: {self.name}"]
        if self.aliases:
            bits.append("aliases=" + "/".join(self.aliases[:4]))
        if self.meta:
            meta = ", ".join(f"{k}={v}" for k, v in sorted(self.meta.items())[:4])
            bits.append(meta)
        return " - ".join(bits)


@dataclass(frozen=True)
class IpProfile:
    ip_id: str
    canonical: str
    aliases: tuple[str, ...] = ()
    entities: tuple[IpEntity, ...] = ()

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "IpProfile | None":
        ip_id = _clean_str(raw.get("ip_id"))
        canonical = _clean_str(raw.get("canonical"))
        if not ip_id or not canonical:
            return None
        entities_raw = raw.get("entities")
        entities: list[IpEntity] = []
        if isinstance(entities_raw, list):
            for item in entities_raw:
                if isinstance(item, dict):
                    entity = IpEntity.from_dict(item)
                    if entity is not None:
                        entities.append(entity)
        return cls(
            ip_id=ip_id,
            canonical=canonical,
            aliases=_clean_tuple(raw.get("aliases")),
            entities=tuple(entities),
        )

    def query_terms(self) -> tuple[str, ...]:
        return (self.ip_id, self.canonical, *self.aliases)

    def matches_any(self, terms: Iterable[str]) -> bool:
        own = {term.casefold() for term in self.query_terms() if term}
        for term in terms:
            cleaned = term.strip().casefold()
            if cleaned and cleaned in own:
                return True
        return False

    def excerpt(self, *, evidence_text: str = "", max_entities: int = 48) -> str:
        if not self.entities:
            return ""
        evidence = evidence_text.casefold()

        def score(entity: IpEntity) -> tuple[int, int, str]:
            hit = 0
            for term in entity.searchable_terms():
                folded = term.casefold()
                if folded and folded in evidence:
                    hit = 1
                    break
            return (-hit, _CATEGORY_ORDER.get(entity.category, 99), entity.name.casefold())

        ordered = sorted(self.entities, key=score)[:max_entities]
        lines = [
            f"IP: {self.canonical} ({self.ip_id})",
            "aliases: " + ", ".join(self.aliases[:10]),
            "entities:",
        ]
        lines.extend(entity.format_line() for entity in ordered)
        return "\n".join(lines)


@dataclass(frozen=True)
class IpEntityCatalog:
    profiles: tuple[IpProfile, ...]

    @classmethod
    def empty(cls) -> "IpEntityCatalog":
        return cls(())

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "IpEntityCatalog":
        ips = raw.get("ips")
        profiles: list[IpProfile] = []
        if isinstance(ips, list):
            for item in ips:
                if isinstance(item, dict):
                    profile = IpProfile.from_dict(item)
                    if profile is not None:
                        profiles.append(profile)
        return cls(tuple(profiles))

    @classmethod
    def from_path(cls, path: str | Path) -> "IpEntityCatalog":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("IP entity catalog JSON must be an object")
        return cls.from_dict(data)

    def match_profile(self, query: str, aliases: tuple[str, ...] = ()) -> IpProfile | None:
        terms = (query, *aliases)
        for profile in self.profiles:
            if profile.matches_any(terms):
                return profile
        return None

    def build_context(
        self,
        query: str,
        *,
        aliases: tuple[str, ...] = (),
        evidence_text: str = "",
        max_entities: int = 48,
    ) -> str:
        profile = self.match_profile(query, aliases)
        if profile is None:
            return ""
        return profile.excerpt(evidence_text=evidence_text, max_entities=max_entities)

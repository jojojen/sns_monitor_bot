"""Extract named entities (IPs / products / sets / creators / events / stores)
from tweet text for the SNS signal classifier's RAG retrieval step.

Two-step strategy:
  1. **Alias substring scan** (zero LLM cost, fast):
     pull all known aliases from the KnowledgeDatabase, substring-match each
     against the tweet text (case-insensitive). Hit count covers anything we
     already have grounded knowledge about.
  2. **LLM NER fallback** (only when step 1 returns nothing useful):
     ask the LLM to extract IP / product / set names. Newly discovered
     entities go to the entity researcher's backfill queue.

The function returns canonical names so downstream retrieval can look them
up directly without ambiguity.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Callable, Iterable, Protocol

logger = logging.getLogger(__name__)


class _AliasSource(Protocol):
    """Minimal interface the extractor needs from KnowledgeDatabase. Defined
    here so the extractor lives in sns_monitor_bot and KnowledgeDatabase
    lives in aka_no_claw — no hard import dependency."""

    def all_aliases(self) -> list[tuple[str, str]]: ...
    def lookup_canonical(self, alias: str) -> str | None: ...


def _build_extraction_prompt(tweet_text: str) -> str:
    return (
        "你是 TCG / 收藏品 SNS 推文的實體抽取器。從以下推文中找出值得記入知識庫的 entity：\n"
        "  - IP 名稱（pokemon / pjsk / ホロライブ / 遊戯王 等）\n"
        "  - 商品 / 套裝名（アビスアイ box / クリムゾンヘイズ / アクスタ）\n"
        "  - 創作者 / 角色名（特定 VTuber / Vocaloid 角色）\n"
        "  - 限時活動（特定抽選名 / 展覽名）\n"
        "  - 店鋪 / 通路（Joshin / カードラッシュ）\n\n"
        "規則：\n"
        "  - 只回真正有獨立辨識性的 entity（不要回「カード」「商品」「抽選」這種通用詞）\n"
        "  - 用推文原語言寫；保留商品 / 角色的正式拼法\n"
        "  - 最多回 6 個\n"
        "  - 推文若沒有任何明確 entity，回空陣列\n\n"
        f"推文：\n\"\"\"\n{tweet_text}\n\"\"\"\n\n"
        "請嚴格回 JSON：\n"
        '{"entities": ["..."]}'
    )


def _parse_llm_entities(raw: str) -> tuple[str, ...]:
    if not raw:
        return ()
    text = raw.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    parsed = None
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError, json.JSONDecodeError):
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if match:
            try:
                parsed = json.loads(match.group(0))
            except (TypeError, ValueError, json.JSONDecodeError):
                pass
    if not isinstance(parsed, dict):
        return ()
    entities = parsed.get("entities") or []
    if not isinstance(entities, list):
        return ()
    out: list[str] = []
    seen: set[str] = set()
    for value in entities:
        if not isinstance(value, str):
            continue
        cleaned = value.strip()
        if not cleaned or cleaned.casefold() in seen:
            continue
        seen.add(cleaned.casefold())
        out.append(cleaned)
        if len(out) >= 6:
            break
    return tuple(out)


def extract_entities(
    tweet_text: str,
    *,
    alias_source: _AliasSource,
    llm_fn: Callable[[str], str] | None = None,
    min_alias_length: int = 2,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Extract entities from ``tweet_text``.

    Returns a tuple ``(known_canonicals, novel_mentions)`` where
      - ``known_canonicals`` = canonical names of entities that matched an
        alias already in the knowledge DB (deduped, order-preserved)
      - ``novel_mentions`` = entities the LLM proposed that did NOT match any
        known alias (caller may enqueue these for ``EntityResearcher``)

    Step 1 (alias scan) is always run. Step 2 (LLM) is only run when step 1
    returned nothing AND an ``llm_fn`` is supplied — gives the user a way to
    cap LLM cost (e.g. disable NER on devices where ollama isn't available).
    """
    if not tweet_text or not tweet_text.strip():
        return (), ()

    text_lower = tweet_text.lower()
    try:
        all_aliases = alias_source.all_aliases()
    except Exception:
        logger.exception("entity_extractor: alias source query failed")
        all_aliases = []

    matched: dict[str, None] = {}  # ordered set via dict insertion order
    for alias, canonical in all_aliases:
        if not alias or len(alias) < min_alias_length:
            continue
        if alias.lower() in text_lower:
            matched.setdefault(canonical, None)
    known_canonicals = tuple(matched.keys())

    novel_mentions: tuple[str, ...] = ()
    if not known_canonicals and llm_fn is not None:
        try:
            raw = llm_fn(_build_extraction_prompt(tweet_text))
        except Exception:
            logger.exception("entity_extractor: LLM extraction failed")
        else:
            extracted = _parse_llm_entities(raw)
            # Map extracted to canonical if possible; remaining are novel.
            novel: list[str] = []
            for entity in extracted:
                canonical = alias_source.lookup_canonical(entity)
                if canonical is None:
                    novel.append(entity)
                else:
                    matched.setdefault(canonical, None)
            known_canonicals = tuple(matched.keys())
            novel_mentions = tuple(novel)

    return known_canonicals, novel_mentions

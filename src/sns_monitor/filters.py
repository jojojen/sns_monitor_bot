from __future__ import annotations

import ast
import json
import re
import shlex
from collections.abc import Iterable

from .models import Tweet

_ACCOUNT_HANDLE_RE = re.compile(r"^@(?P<handle>[A-Za-z0-9_]{1,15})(?:\s+(?P<filters>.*))?$")
_FILTER_PREFIX_RE = re.compile(r"^(?:--)?(?:include-)?(?:keywords?|filters?)\s*[:=]?\s*", re.IGNORECASE)
_BRACKETED_FILTER_RE = re.compile(r"[\[\(]([^\]\)]+)[\]\)]")
# Captures `filter[...]` / `domain[...]` labelled brackets (case-insensitive,
# half/full-width bracket pairs handled via the same translation table).
_LABELED_BRACKET_RE = re.compile(
    r"\b(filter|filters|fil|domain|domains|dom)\s*[\[\(]\s*([^\]\)]*)\s*[\]\)]",
    re.IGNORECASE,
)
_FILTER_NOISE_RE = re.compile(
    r"\b(?:add|only|notify|with|filter|filters|keyword|keywords)\b|加上|加入|新增|只看|只通知|包含|提到|相關|關鍵字|关键词|篩選|过滤|過濾",
    re.IGNORECASE,
)
_TRANSLATION_TABLE = str.maketrans({
    "［": "[",
    "］": "]",
    "【": "[",
    "】": "]",
    "（": "(",
    "）": ")",
    "｛": "{",
    "｝": "}",
    "，": ",",
    "、": ",",
    "：": ":",
    "；": ";",
    "「": '"',
    "」": '"',
    "『": '"',
    "』": '"',
    "“": '"',
    "”": '"',
    "‘": "'",
    "’": "'",
})


def normalize_keyword_filters(values: str | Iterable[str] | None) -> tuple[str, ...]:
    """Normalize keyword filter input while preserving display casing."""
    if values is None:
        return ()
    chunks = [values] if isinstance(values, str) else list(values)
    if len(chunks) > 1 and str(chunks[0]).strip().startswith("[") and str(chunks[-1]).strip().endswith("]"):
        parsed = _parse_list_literal(" ".join(str(chunk) for chunk in chunks))
        if parsed is not None:
            chunks = [str(item) for item in parsed]

    keywords: list[str] = []
    seen: set[str] = set()
    for chunk in chunks:
        for keyword in _expand_keyword_chunk(str(chunk)):
            normalized = " ".join(keyword.strip().split())
            if not normalized:
                continue
            key = normalized.casefold()
            if key in seen:
                continue
            seen.add(key)
            keywords.append(normalized)
    return tuple(keywords)


def parse_keyword_filter_text(text: str | None) -> tuple[str, ...]:
    """Parse Telegram-style keyword filter text into normalized keywords."""
    if not text:
        return ()

    standardized = _standardize_filter_text(text)
    bracketed = _extract_bracketed_filters(standardized)
    if bracketed:
        return normalize_keyword_filters(bracketed)

    cleaned = _strip_filter_prefix(standardized)
    cleaned = _FILTER_NOISE_RE.sub(" ", cleaned)
    cleaned = " ".join(cleaned.split())
    if not cleaned:
        return ()
    if cleaned.startswith("[") or "," in cleaned:
        return normalize_keyword_filters(cleaned)

    try:
        parts = shlex.split(cleaned)
    except ValueError:
        parts = cleaned.split()
    return normalize_keyword_filters(parts)


def extract_labeled_brackets(raw: str) -> tuple[tuple[str, ...] | None, tuple[str, ...] | None, str]:
    """Pull `filter[...]` and `domain[...]` labelled brackets out of *raw*.

    Returns a triple ``(filter_keywords, domains, remainder)`` where each of
    the first two is ``None`` when the user did not include that bracket
    label at all (caller can preserve the existing rule field in that case),
    or a tuple of strings (possibly empty) when the bracket was provided.

    The remainder is the input string with the labelled brackets removed
    so the existing handle / keyword / trend parsing can run on it.
    """
    if not raw:
        return None, None, ""
    standardized = _standardize_filter_text(raw)
    captured: dict[str, list[str]] = {"filter": [], "domain": []}
    seen_labels: set[str] = set()

    def _consume(match: "re.Match[str]") -> str:
        label = match.group(1).lower()
        canonical = "filter" if label.startswith("fil") else "domain"
        seen_labels.add(canonical)
        content = match.group(2)
        if content.strip():
            captured[canonical].extend(_split_filter_values(content))
        return " "

    remainder = _LABELED_BRACKET_RE.sub(_consume, standardized)
    filter_keywords: tuple[str, ...] | None = None
    domains: tuple[str, ...] | None = None
    if "filter" in seen_labels:
        filter_keywords = normalize_keyword_filters(captured["filter"])
    if "domain" in seen_labels:
        from .models import normalize_domains

        domains = normalize_domains(captured["domain"])
    cleaned_remainder = " ".join(remainder.split())
    return filter_keywords, domains, cleaned_remainder


def parse_account_watch_text(
    raw: str,
) -> tuple[str, tuple[str, ...], tuple[str, ...] | None] | None:
    """Parse '@handle [optional filter[...]/domain[...] or legacy ["a","b"]]'.

    Returns ``(handle, include_keywords, domains)`` where ``domains`` is
    ``None`` if the user did NOT supply a ``domain[...]`` bracket (caller
    should preserve the existing rule's domains in that case).
    """
    explicit_filter, domains, remainder = extract_labeled_brackets(raw.strip())
    match = _ACCOUNT_HANDLE_RE.match(remainder)
    if match is None:
        return None
    handle = match.group("handle")
    if explicit_filter is not None:
        include_keywords = explicit_filter
    else:
        include_keywords = parse_keyword_filter_text(match.group("filters"))
    return handle, include_keywords, domains


def tweet_matches_keyword_filters(tweet: Tweet, include_keywords: tuple[str, ...]) -> bool:
    """Return True when a tweet should notify for the given include-keywords filter."""
    if not include_keywords:
        return True
    text = tweet.text.casefold()
    return any(keyword.casefold() in text for keyword in include_keywords)


def filter_tweets_by_keywords(tweets: Iterable[Tweet], include_keywords: tuple[str, ...]) -> list[Tweet]:
    return [tweet for tweet in tweets if tweet_matches_keyword_filters(tweet, include_keywords)]


def _expand_keyword_chunk(chunk: str) -> list[str]:
    cleaned = _strip_filter_prefix(_standardize_filter_text(chunk).strip())
    bracketed = _extract_bracketed_filters(cleaned)
    if bracketed:
        return bracketed
    if not cleaned:
        return []

    parsed = _parse_list_literal(cleaned)
    if parsed is not None:
        return [str(item) for item in parsed]

    if "," in cleaned:
        return cleaned.split(",")
    return [cleaned]


def _parse_list_literal(value: str) -> list[object] | None:
    if not value.startswith("["):
        return None
    for parser in (json.loads, ast.literal_eval):
        try:
            parsed = parser(value)
        except (ValueError, SyntaxError, TypeError, json.JSONDecodeError):
            continue
        if isinstance(parsed, (list, tuple)):
            return list(parsed)
    return None


def _strip_filter_prefix(value: str) -> str:
    return _FILTER_PREFIX_RE.sub("", value.strip(), count=1).strip()


def _standardize_filter_text(value: str) -> str:
    return value.translate(_TRANSLATION_TABLE)


def _extract_bracketed_filters(value: str) -> list[str]:
    matches = _BRACKETED_FILTER_RE.findall(value)
    if not matches:
        return []
    keywords: list[str] = []
    for match in matches:
        keywords.extend(_split_filter_values(match))
    return keywords


def _split_filter_values(value: str) -> list[str]:
    parsed = _parse_list_literal(value if value.startswith("[") else f"[{value}]")
    if parsed is not None:
        return [str(item) for item in parsed]
    if "," in value:
        return [part for part in value.split(",")]
    return [value]

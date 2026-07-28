"""Frequency + meaning enrichment.

Rules-only by default (no fabrication). An LLM fallback hook is exposed but
left unimplemented here - see design.md open questions (LLM provider choice).
"""
from __future__ import annotations

from typing import Optional

# (keywords, frequency) - first match wins.
_FREQUENCY_RULES: list[tuple[tuple[str, ...], str]] = [
    (("实时", "spot", "live", "intraday", "minute", "分时"), "daily"),
    (("历史", "hist", "daily", "日行情", "日线"), "daily"),
    (("周", "weekly"), "weekly"),
    (("月", "month"), "monthly"),
    (("季", "quarter"), "quarterly"),
    (("年报", "annual", "yearly", "年度"), "yearly"),
    (("基金净值", "nav"), "daily"),
]


def derive_frequency(category: Optional[str], description: Optional[str]) -> str:
    """Infer a frequency token from category + description text. 'unknown' if no match."""
    text = " ".join(filter(None, [category or "", description or ""])).lower()
    if not text.strip():
        return "unknown"
    for keys, freq in _FREQUENCY_RULES:
        if any(k.lower() in text for k in keys):
            return freq
    return "unknown"


def derive_meaning(description: Optional[str]) -> str:
    """Return a meaning hint from a column description.

    Empty / "-" descriptions yield 'unknown' (never fabricated). Non-empty
    descriptions are passed through as the meaning hint.
    """
    if description is None:
        return "unknown"
    d = description.strip()
    if not d or d == "-":
        return "unknown"
    return d


def enrich_frequency_llm(category: Optional[str], description: Optional[str]) -> str:
    """LLM fallback hook - unimplemented in v1. Returns 'unknown'."""
    # See design.md open question: LLM provider for meaning-enrichment.
    return "unknown"

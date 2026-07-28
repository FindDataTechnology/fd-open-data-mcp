"""Concept mapper: propose a concept for a physical column.

v1 uses a rule table mapping well-known column names (zh/en) and cn-gov
``semantic_type`` hints to concept codes. Each rule carries a ``measure``
(``nominal_current``/``real_constant``/``ppp``/``per_capita``/``growth``, or
``""`` when N/A) so that same-family-different-measure columns (GDP nominal vs
PPP vs per-capita) bind to **distinct** concepts (design.md D1).

An LLM fallback hook is exposed (``propose_concept_llm``) but unimplemented.
"""
from __future__ import annotations

from typing import Optional

# Dispatch confidence threshold. Bindings below this are withheld from
# dispatch and held in the review queue (design.md D4).
DEFAULT_THRESHOLD = 0.6

# (column-name patterns, (code, entity_type, MEASURE, unit, frequency), confidence)
_RULES: list[tuple[tuple[str, ...], tuple[str, str, str, str, str], float]] = [
    (("收盘", "close", "adj close"), ("price.close", "stock", "", "currency", "daily"), 0.9),
    (("开盘", "open"), ("price.open", "stock", "", "currency", "daily"), 0.9),
    (("最高", "high"), ("price.high", "stock", "", "currency", "daily"), 0.9),
    (("最低", "low"), ("price.low", "stock", "", "currency", "daily"), 0.9),
    (("成交量", "volume"), ("price.volume", "stock", "", "shares", "daily"), 0.9),
    (("成交额", "amount", "turnover"), ("price.amount", "stock", "", "currency", "daily"), 0.85),
    # edgar / financial-statement line items
    (("revenue", "total revenue", "营业收入"), ("financials.revenue", "stock", "", "currency", "yearly"), 0.85),
    (("net income", "净利润"), ("financials.net_income", "stock", "", "currency", "yearly"), 0.85),
    (("total assets", "总资产"), ("financials.total_assets", "stock", "", "currency", "yearly"), 0.85),
    (("total liab", "总负债"), ("financials.total_liabilities", "stock", "", "currency", "yearly"), 0.85),
    (("operating cash flow", "经营现金流"), ("financials.operating_cash_flow", "stock", "", "currency", "yearly"), 0.85),
    # cn-report Chinese financial-statement terms
    (("资产总计",), ("financials.total_assets", "stock", "", "currency", "yearly"), 0.9),
    (("负债合计",), ("financials.total_liabilities", "stock", "", "currency", "yearly"), 0.9),
    (("所有者权益合计", "股东权益合计"), ("financials.equity", "stock", "", "currency", "yearly"), 0.9),
    (("经营活动现金流量",), ("financials.operating_cash_flow", "stock", "", "currency", "yearly"), 0.9),
    # wbgapi / World Bank indicator codes (measure disambiguates the GDP variants)
    # N.B. longer/more-specific patterns must precede shorter ones (".kd.zg" before ".kd")
    (("ny.gdp.mktp.kd.zg",), ("gdp", "country", "growth", "%", "yearly"), 0.9),
    (("ny.gdp.mktp.kd",), ("gdp", "country", "real_constant", "usd", "yearly"), 0.9),
    (("ny.gdp.mktp.cd",), ("gdp", "country", "nominal_current", "usd", "yearly"), 0.9),
    (("ny.gdp.pcap.pp.cd",), ("gdp", "country", "per_capita_ppp", "international", "yearly"), 0.9),
    (("ny.gdp.pcap.cd",), ("gdp", "country", "per_capita", "usd", "yearly"), 0.9),
    (("sp.pop.totl",), ("population.total", "country", "", "persons", "yearly"), 0.9),
    (("en.atm.co2e.kt",), ("co2_emissions", "country", "", "kt", "yearly"), 0.9),
    (("en.atm.co2e.pc",), ("co2.per_capita", "country", "", "kt", "yearly"), 0.85),
    (("fp.cpi.totl.zg",), ("inflation", "country", "", "%", "yearly"), 0.85),
    (("sl.uem.totl.zs",), ("unemployment", "country", "", "%", "yearly"), 0.85),
]


def propose_concept(
    column_name: str,
    column_description: Optional[str] = None,
    semantic_type: Optional[str] = None,
) -> Optional[dict]:
    """Return a concept proposal {code, entity_type, measure, unit, frequency, confidence} or None."""
    name = (column_name or "").lower()
    sem = (semantic_type or "").lower().strip()

    # cn-gov semantic_type hints -> generic document concepts
    if sem == "title":
        return {"code": "doc.title", "entity_type": "industry", "measure": "", "unit": "", "frequency": "irregular", "confidence": 0.8}
    if sem == "date":
        return {"code": "doc.date", "entity_type": "industry", "measure": "", "unit": "", "frequency": "irregular", "confidence": 0.8}
    if sem == "url":
        return {"code": "doc.url", "entity_type": "industry", "measure": "", "unit": "", "frequency": "irregular", "confidence": 0.8}

    for patterns, (code, etype, measure, unit, freq), conf in _RULES:
        if any(p in name for p in patterns):
            return {"code": code, "entity_type": etype, "measure": measure,
                    "unit": unit, "frequency": freq, "confidence": conf}
    return None


def propose_concept_llm(
    column_name: str,
    column_description: Optional[str] = None,
    semantic_type: Optional[str] = None,
) -> Optional[dict]:
    """LLM fallback hook - unimplemented in v1. Returns None (no proposal)."""
    # See design.md open question: LLM provider for concept-mapping.
    return None

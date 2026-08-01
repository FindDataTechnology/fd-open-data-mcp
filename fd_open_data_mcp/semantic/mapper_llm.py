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
    # additional cn-report income-statement / balance-sheet / cash-flow line items
    (("基本每股收益", "每股收益"), ("financials.eps", "stock", "", "currency", "yearly"), 0.85),
    (("营业总收入",), ("financials.revenue", "stock", "", "currency", "yearly"), 0.9),
    (("营业总成本",), ("financials.total_cost", "stock", "", "currency", "yearly"), 0.85),
    (("营业利润",), ("financials.operating_profit", "stock", "", "currency", "yearly"), 0.85),
    (("利润总额",), ("financials.total_profit", "stock", "", "currency", "yearly"), 0.85),
    (("投资收益",), ("financials.investment_income", "stock", "", "currency", "yearly"), 0.85),
    (("固定资产",), ("financials.fixed_assets", "stock", "", "currency", "yearly"), 0.85),
    (("销售商品、提供劳务收到的现金", "销售商品提供劳务收到的现金"), ("financials.cash_received_from_sales", "stock", "", "currency", "yearly"), 0.85),
    (("购买商品、接收劳务支付的现金", "购买商品接受劳务支付的现金"), ("financials.cash_paid_for_goods", "stock", "", "currency", "yearly"), 0.85),
    (("取得借款收到的现金",), ("financials.cash_from_borrowing", "stock", "", "currency", "yearly"), 0.85),
    (("应收票据及应收账款", "应收账款"), ("financials.accounts_receivable", "stock", "", "currency", "yearly"), 0.85),
    (("应付票据及应付账款", "应付账款"), ("financials.accounts_payable", "stock", "", "currency", "yearly"), 0.85),
    (("预付款项",), ("financials.prepayments", "stock", "", "currency", "yearly"), 0.85),
    (("合同负债",), ("financials.contract_liabilities", "stock", "", "currency", "yearly"), 0.85),
    (("未分配利润",), ("financials.retained_earnings", "stock", "", "currency", "yearly"), 0.85),
    (("股本",), ("financials.share_capital", "stock", "", "currency", "yearly"), 0.85),
    (("综合收益总额",), ("financials.comprehensive_income", "stock", "", "currency", "yearly"), 0.85),
    (("资产负债率",), ("financials.debt_ratio", "stock", "", "%", "yearly"), 0.8),
    (("其他应收款",), ("financials.other_receivables", "stock", "", "currency", "yearly"), 0.85),
    (("其他应付款",), ("financials.other_payables", "stock", "", "currency", "yearly"), 0.85),
    (("业务及管理费用",), ("financials.admin_expense", "stock", "", "currency", "yearly"), 0.85),
    (("税金及附加",), ("financials.taxes_surcharge", "stock", "", "currency", "yearly"), 0.85),
    (("所得税费用",), ("financials.income_tax", "stock", "", "currency", "yearly"), 0.85),
    (("营业外收入",), ("financials.non_op_income", "stock", "", "currency", "yearly"), 0.85),
    (("营业外支出",), ("financials.non_op_expense", "stock", "", "currency", "yearly"), 0.85),
    (("其他收益",), ("financials.other_income", "stock", "", "currency", "yearly"), 0.85),
    (("利息收入",), ("financials.interest_income", "stock", "", "currency", "yearly"), 0.85),
    (("信用减值损失",), ("financials.credit_impairment_loss", "stock", "", "currency", "yearly"), 0.85),
    (("资产处置收益",), ("financials.asset_disposal_income", "stock", "", "currency", "yearly"), 0.85),
    (("其他流动资产",), ("financials.other_current_assets", "stock", "", "currency", "yearly"), 0.85),
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
    (("ne.exp.gnfs.zs",), ("exports", "country", "", "%", "yearly"), 0.85),
    (("ne.imp.gnfs.zs",), ("imports", "country", "", "%", "yearly"), 0.85),
    (("sp.dyn.le00.in",), ("life_expectancy", "country", "", "years", "yearly"), 0.85),
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

"""Concept mapper: propose a concept for a physical column.

v1 uses a rule table mapping well-known column names (zh/en) and cn-gov
``semantic_type`` hints to concept codes. Each rule carries a ``measure``
(``nominal_current``/``real_constant``/``ppp``/``per_capita``/``growth``, or
``""`` when N/A) so that same-family-different-measure columns (GDP nominal vs
PPP vs per-capita) bind to **distinct** concepts (design.md D1).

An LLM fallback hook is exposed (``propose_concept_llm``) but unimplemented.

Matching is on the column NAME alone, which is why a domain check guards it: a
name like ``收盘`` occurs in stock, option, futures, bond and fund endpoints
alike, so matching on it alone bound all of them to `price.close`/`stock`
(concept 234 reached 117 dispatch-eligible bindings, two of which accounted for
every fetch the fleet made on 2026-09-16). A proposal is now refused when the
function's declared domain and the rule's concept domain disagree — see
:func:`function_domain` and :func:`propose_concept`.
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

# cn-gov semantic_type hints -> generic document concepts. These carry no
# instrument domain, so they are never domain-checked.
_SEMANTIC_TYPE_RULES: dict[str, tuple[str, str, str, str, str]] = {
    "title": ("doc.title", "industry", "", "", "irregular"),
    "date": ("doc.date", "industry", "", "", "irregular"),
    "url": ("doc.url", "industry", "", "", "irregular"),
}

# Entity domain declared by a function's command prefix. Checked most-specific
# first: a name that is prefixed `stock_` but is really an index endpoint is an
# exception, not a stock function.
_DOMAIN_PREFIXES: tuple[tuple[str, str], ...] = (
    ("option_", "future"),
    ("futures_", "future"),
    ("bond_", "bond"),
    ("fund_", "fund"),
    ("index_", "index"),
    ("stock_", "stock"),
)

# akshare names a few index endpoints with a `stock_` prefix.
_DOMAIN_PREFIX_EXCEPTIONS: tuple[tuple[str, str], ...] = (
    ("stock_zh_index_", "index"),
    ("stock_hk_index_", "index"),
    ("stock_us_index_", "index"),
)

# Sources whose every endpoint serves one entity domain, used when the command
# name declares nothing. This is still positive evidence — "this source only
# serves country indicators" is a fact about the source, not the absence of a
# contradiction — and without it the domain rule cannot confirm the 103
# bindings whose commands (`get_indicator_data`, `mee_tzgg_archive`) carry no
# prefix, emptying nine concepts.
_SOURCE_DOMAIN: dict[str, str] = {
    "wbgapi": "country",
    "worldbank": "country",
    "cn-gov": "industry",       # government notices; doc.* concepts are `industry`
    "fd-cn-gov": "industry",
    "yfinance": "stock",
    "edgar": "stock",           # its financials.* rules bind `stock`
}


# A source-level default is coarse: yfinance serves stock prices AND option
# chains, so `ticker_option_chain` would inherit `stock` and its `volume` column
# would be read as the underlying's traded volume. A command NAME that carries a
# different instrument family overrides the source default.
_DOMAIN_MARKERS: tuple[tuple[str, str], ...] = (
    ("option", "future"),
    # akshare ships more `stock_`-prefixed index endpoints than the exception
    # table above lists (`stock_board_concept_index_ths`,
    # `stock_buffett_index_lg`) — their "close" is an index level, not a share
    # price, so a name carrying `index` is not a stock function.
    ("index", "index"),
)


def function_domain(
    command: Optional[str], source: Optional[str] = None,
) -> Optional[str]:
    """The entity domain a function declares, from its command name or its source.

    A function's name says what instrument family it returns: `option_czce_hist`
    returns options, `futures_zh_spot` futures, `bond_zh_hs_daily` bonds. When
    the name declares nothing (a provider command, a document fetch) the source's
    domain is used. Returns None when neither declares one — an undeclared domain
    is not evidence of a conflict, so such columns are checked as before.
    """
    if command:
        lowered = command.lower()
        # Most specific first: an explicit exception, then an instrument word
        # anywhere in the name, then the general family prefix. Markers MUST
        # precede prefixes or `stock_board_industry_index_ths` is claimed by the
        # `stock_` prefix before its `index` is ever seen.
        for prefix, domain in _DOMAIN_PREFIX_EXCEPTIONS:
            if lowered.startswith(prefix):
                return domain
        for marker, domain in _DOMAIN_MARKERS:
            if marker in lowered:
                return domain
        for prefix, domain in _DOMAIN_PREFIXES:
            if lowered.startswith(prefix):
                return domain
    if source:
        return _SOURCE_DOMAIN.get(source.lower())
    return None

def _match_rule(column_name: Optional[str], semantic_type: Optional[str]):
    """First matching (rule spec, confidence) for a column, or (None, None)."""
    sem = (semantic_type or "").lower().strip()
    if sem in _SEMANTIC_TYPE_RULES:
        return _SEMANTIC_TYPE_RULES[sem], 0.8
    name = (column_name or "").lower()
    for patterns, spec, conf in _RULES:
        if any(p in name for p in patterns):
            return spec, conf
    return None, None


def domain_conflict(
    column_name: Optional[str],
    semantic_type: Optional[str] = None,
    command: Optional[str] = None,
    source: Optional[str] = None,
) -> Optional[dict]:
    """The cross-domain refusal a rule-derived proposal would hit, for reporting.

    Returns None when there is no conflict — including when no rule matches at
    all, since that is not a domain problem.
    """
    spec, _conf = _match_rule(column_name, semantic_type)
    if spec is None:
        return None
    code, entity_type = spec[0], spec[1]
    declared = function_domain(command, source)
    if declared is None or declared == entity_type:
        return None
    return {
        "command": command, "column": column_name, "concept": code,
        "concept_entity_type": entity_type, "function_domain": declared,
    }


def propose_concept(
    column_name: str,
    column_description: Optional[str] = None,
    semantic_type: Optional[str] = None,
    command: Optional[str] = None,
    source: Optional[str] = None,
) -> Optional[dict]:
    """Return a concept proposal {code, entity_type, measure, unit, frequency, confidence} or None.

    ``command`` and ``source`` identify the function. When either declares an
    entity domain (see :func:`function_domain`) and the matched rule's concept
    belongs to a different one, the proposal is refused — returned as None, so no
    binding is created. This is what stops an options function's 收盘 column from
    becoming a stock closing price.
    """
    spec, conf = _match_rule(column_name, semantic_type)
    if spec is None:
        return None
    code, entity_type, measure, unit, frequency = spec
    declared = function_domain(command, source)
    if declared is not None and declared != entity_type:
        return None
    return {"code": code, "entity_type": entity_type, "measure": measure,
            "unit": unit, "frequency": frequency, "confidence": conf}


def propose_concept_llm(
    column_name: str,
    column_description: Optional[str] = None,
    semantic_type: Optional[str] = None,
) -> Optional[dict]:
    """LLM fallback hook - unimplemented in v1. Returns None (no proposal)."""
    # See design.md open question: LLM provider for concept-mapping.
    return None

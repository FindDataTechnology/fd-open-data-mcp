"""Curated seed of dartlab (``dartlab``) commands.

dartlab (PyPI dist ``dartlab``) imports as ``dartlab``; requires **Python 3.12**.
Its Korean corporate-data API is centred on ``Company`` — a factory function
(not a class) routing via a provider ``canHandle`` chain:

  - ``dartlab.Company(code)`` accepts 종목코드 ("005930"), 회사명 ("삼성전자"),
    or a US ticker ("AAPL"); DART→EDGAR auto-routing. Returns a company proxy.
  - ``company.panel`` is a callable property returning a ``Panel``
    (``pl.DataFrame`` subclass, wide: 항목 rows × period columns).
  - ``company.credit`` / ``company.analysis`` are likewise callable properties
    returning dicts (rating / 22-axis analysis).
  - ``company.news(*, days=30)`` → ``pl.DataFrame`` (keyless RSS).
  - ``company.disclosure(start, end, ...)`` → ``pl.DataFrame``
    (requires ``DART_API_KEY``).
  - ``Company.search(keyword, *, limit=None)`` staticmethod → ``pl.DataFrame``
    (keyless KIND listing).

Results are **polars** DataFrames (coerced to pandas via ``.to_pandas()``);
``credit``/``analysis`` return dicts. The DART credential is
``DART_API_KEY`` (alt ``DART_API_KEYS``), read **directly** by dartlab — no
``configure()`` call (unlike edinet-tools).

This seed defines the key commands the ontology dispatches to. It lives inside
``fd-open-data-mcp`` so catalog import works **without** the ``data`` extra
installed — only upstream introspection + fetch require the ``dartlab`` package.
"""
from __future__ import annotations

DART_SOURCE = "https://opendart.fss.or.kr/"

_CODE_PARAM = {
    "name": "code",
    "type": "str",
    "required": True,
    "description": "Korean 종목코드 ('005930'), 회사명 ('삼성전자'), or US ticker ('AAPL')",
}

# Wide-panel row-identity columns (period columns are dynamic: '2025Q4'/'2024').
_PANEL_IDENTITY_COLUMNS = [
    {"name": "canonicalKey", "type": "str", "description": "Account canonical key (row identity)"},
    {"name": "sectionLeaf", "type": "str", "description": "Section leaf label"},
    {"name": "blockLeaf", "type": "str", "description": "Block leaf label"},
    {"name": "항목", "type": "str", "description": "Korean account name (row label)"},
]

_CREDIT_COLUMNS = [
    {"name": "grade", "type": "str", "description": "dCR credit grade"},
    {"name": "score", "type": "float", "description": "Credit score"},
    {"name": "healthScore", "type": "float", "description": "Health score"},
    {"name": "outlook", "type": "str", "description": "Outlook"},
]

_NEWS_COLUMNS = [
    {"name": "title", "type": "str", "description": "Headline"},
    {"name": "date", "type": "str", "description": "Publish date"},
    {"name": "source", "type": "str", "description": "Source outlet"},
    {"name": "link", "type": "str", "description": "Article URL"},
]

_DISCLOSURE_COLUMNS = [
    {"name": "docId", "type": "str", "description": "DART document id"},
    {"name": "filedAt", "type": "str", "description": "Filing datetime"},
    {"name": "title", "type": "str", "description": "Filing title"},
    {"name": "formType", "type": "str", "description": "Form type"},
]

_SEARCH_COLUMNS = [
    {"name": "stockCode", "type": "str", "description": "종목코드"},
    {"name": "corpName", "type": "str", "description": "회사명"},
    {"name": "market", "type": "str", "description": "Market (KOSPI/KOSDAQ)"},
    {"name": "sector", "type": "str", "description": "Sector"},
]

REGISTRY: dict[str, dict] = {
    "company_panel": {
        "category": "financials",
        "description": "Wide accounting panel (항목 × period) for a Korean company; period columns are dynamic ('2025Q4'/'2024')",
        "source": DART_SOURCE,
        "parameters": [
            _CODE_PARAM,
            {"name": "key", "type": "str", "required": False,
             "description": "Panel key: lowercase is/bs/cf/cis/sce = native, UPPERCASE = finance injection, 'ratios' = native ratios, None = full grid"},
            {"name": "freq", "type": "str", "required": False,
             "description": "Frequency: 'year' / 'quarter' / 'ytd'"},
        ],
        "columns": _PANEL_IDENTITY_COLUMNS,
    },
    "company_credit": {
        "category": "credit",
        "description": "dCR credit rating (20-grade, 7 axes) for a Korean company",
        "source": DART_SOURCE,
        "parameters": [
            _CODE_PARAM,
            {"name": "axis", "type": "str", "required": False,
             "description": "Specific axis label, e.g. '등급'"},
            {"name": "detail", "type": "bool", "required": False,
             "description": "Return full detail dict (grade/score/healthScore/axes/outlook)"},
        ],
        "columns": _CREDIT_COLUMNS,
    },
    "company_analysis": {
        "category": "analysis",
        "description": "22-axis / 5-group financial analysis for a Korean company (returns dict or pl.DataFrame)",
        "source": DART_SOURCE,
        "parameters": [
            _CODE_PARAM,
            {"name": "axis", "type": "str", "required": False,
             "description": "Analysis group, e.g. 'financial'"},
            {"name": "subaxis", "type": "str", "required": False,
             "description": "Sub-axis, e.g. '수익성' (profitability)"},
        ],
        "columns": [
            {"name": "axis", "type": "str", "description": "Analysis axis name"},
            {"name": "value", "type": "float", "description": "Axis value/score"},
        ],
    },
    "company_news": {
        "category": "news",
        "description": "Recent news (RSS) for a Korean company (keyless, no DART_API_KEY)",
        "source": DART_SOURCE,
        "parameters": [
            _CODE_PARAM,
            {"name": "days", "type": "int", "required": False,
             "description": "Lookback window in days (default 30)"},
        ],
        "columns": _NEWS_COLUMNS,
    },
    "company_disclosure": {
        "category": "filings",
        "description": "DART filings for a Korean company in a date window (requires DART_API_KEY)",
        "source": DART_SOURCE,
        "parameters": [
            _CODE_PARAM,
            {"name": "start", "type": "str", "required": True,
             "description": "Window start YYYY-MM-DD"},
            {"name": "end", "type": "str", "required": True,
             "description": "Window end YYYY-MM-DD"},
            {"name": "type", "type": "str", "required": False,
             "description": "Filing type filter"},
            {"name": "keyword", "type": "str", "required": False,
             "description": "Keyword filter"},
            {"name": "finalOnly", "type": "bool", "required": False,
             "description": "Final filings only"},
        ],
        "columns": _DISCLOSURE_COLUMNS,
    },
    "company_search": {
        "category": "entities",
        "description": "Search Korean companies by keyword (keyless KIND listing, no DART_API_KEY)",
        "source": DART_SOURCE,
        "parameters": [
            {"name": "keyword", "type": "str", "required": True,
             "description": "Company name substring, e.g. '삼성'"},
            {"name": "limit", "type": "int", "required": False,
             "description": "Max results"},
        ],
        "columns": _SEARCH_COLUMNS,
    },
}

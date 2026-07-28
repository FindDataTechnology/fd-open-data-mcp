"""Curated seed of edgartools (``edgar``) commands.

edgartools (PyPI dist ``edgartools``) imports as ``edgar``; its API is
object-oriented around ``Company`` (like yfinance's ``Ticker``). This seed
defines the key commands the ontology dispatches to. It lives inside
``fd-open-data-mcp`` so catalog import works **without** the ``data`` extra
installed - only upstream introspection + fetch require the ``edgartools``
package.
"""
from __future__ import annotations

EDGAR_SOURCE = "https://www.sec.gov/edgar"

_SYMBOL_PARAM = {
    "name": "ticker", "type": "str", "required": True,
    "description": "US stock ticker, e.g. 'AAPL', 'MSFT'",
}

# Financial line items the runner extracts from get_financials() / statements.
_FINANCIAL_COLUMNS = [
    {"name": "Revenue", "type": "float", "description": "Total revenue"},
    {"name": "Net Income", "type": "float", "description": "Net income"},
    {"name": "Total Assets", "type": "float", "description": "Total assets"},
    {"name": "Total Liabilities", "type": "float", "description": "Total liabilities"},
    {"name": "Operating Cash Flow", "type": "float", "description": "Cash from operating activities"},
]

REGISTRY: dict[str, dict] = {
    "company_get_financials": {
        "category": "financials",
        "description": "Company financial statements (income / balance sheet / cash flow)",
        "source": EDGAR_SOURCE,
        "parameters": [_SYMBOL_PARAM],
        "columns": _FINANCIAL_COLUMNS,
    },
    "company_income_statement": {
        "category": "financials",
        "description": "Income statement (revenue, expenses, profit)",
        "source": EDGAR_SOURCE,
        "parameters": [_SYMBOL_PARAM],
        "columns": [
            {"name": "Revenue", "type": "float", "description": "Total revenue"},
            {"name": "Net Income", "type": "float", "description": "Net income"},
        ],
    },
    "company_balance_sheet": {
        "category": "financials",
        "description": "Balance sheet (assets, liabilities, equity)",
        "source": EDGAR_SOURCE,
        "parameters": [_SYMBOL_PARAM],
        "columns": [
            {"name": "Total Assets", "type": "float", "description": "Total assets"},
            {"name": "Total Liabilities", "type": "float", "description": "Total liabilities"},
        ],
    },
    "company_cashflow_statement": {
        "category": "financials",
        "description": "Cash flow statement (operating / investing / financing)",
        "source": EDGAR_SOURCE,
        "parameters": [_SYMBOL_PARAM],
        "columns": [
            {"name": "Operating Cash Flow", "type": "float", "description": "Cash from operating activities"},
        ],
    },
    "company_get_filings": {
        "category": "filings",
        "description": "SEC filings for a company, optionally filtered by form (10-K, 10-Q, 8-K)",
        "source": EDGAR_SOURCE,
        "parameters": [
            _SYMBOL_PARAM,
            {"name": "form", "type": "str", "required": False, "description": "Filing form, e.g. '10-K'"},
        ],
        "columns": [
            {"name": "form", "type": "str", "description": "Filing form type"},
            {"name": "filing_date", "type": "str", "description": "Filing date YYYY-MM-DD"},
            {"name": "company", "type": "str", "description": "Company name"},
        ],
    },
    "company_get_facts": {
        "category": "financials",
        "description": "Multi-year XBRL facts for a company",
        "source": EDGAR_SOURCE,
        "parameters": [_SYMBOL_PARAM],
        "columns": [
            {"name": "Revenue", "type": "float", "description": "Multi-year revenue"},
            {"name": "Total Assets", "type": "float", "description": "Multi-year total assets"},
        ],
    },
}

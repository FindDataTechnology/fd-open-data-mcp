"""Curated seed of fd-cn-report (China financial reports) tool output columns.

fd-cn-report is a FastMCP server; MCP introspection captures only tool names
(no output schemas). This seed documents each key tool's output columns so
concept bindings can form. The extraction rules themselves (llm_rules /
script_rules) are exposed separately via `catalog.cnreport_rules`.
"""
from __future__ import annotations

CN_REPORT_SOURCE = "https://github.com/FindDataOfficial/fd-cn-report"

# Common CN financial-statement line items (match mapper financials.* rules).
_FINANCIAL_COLUMNS = [
    {"name": "营业收入", "type": "float", "description": "Operating revenue"},
    {"name": "净利润", "type": "float", "description": "Net income"},
    {"name": "资产总计", "type": "float", "description": "Total assets"},
    {"name": "负债合计", "type": "float", "description": "Total liabilities"},
    {"name": "所有者权益合计", "type": "float", "description": "Total equity"},
    {"name": "经营活动现金流量", "type": "float", "description": "Operating cash flow"},
]

REGISTRY: dict[str, dict] = {
    "get_financial_statements": {
        "category": "financials",
        "description": "Extract the three major financial statements (三大报表) for a CN company",
        "source": CN_REPORT_SOURCE,
        "parameters": [{"name": "ticker", "type": "str", "required": True, "description": "6-digit CN stock code"}],
        "columns": _FINANCIAL_COLUMNS,
    },
    "get_financials": {
        "category": "financials",
        "description": "Structured income/balance/cashflow statements for a CN company",
        "source": CN_REPORT_SOURCE,
        "parameters": [{"name": "ticker", "type": "str", "required": True}],
        "columns": _FINANCIAL_COLUMNS,
    },
    "extract_indicators": {
        "category": "indicators",
        "description": "Extract many financial indicators for one company/year via the rule set",
        "source": CN_REPORT_SOURCE,
        "parameters": [
            {"name": "ticker", "type": "str", "required": True},
            {"name": "year", "type": "int", "required": False},
        ],
        "columns": [
            {"name": "indicator", "type": "str", "description": "Indicator name (e.g. 资产总计)"},
            {"name": "value", "type": "float", "description": "Extracted value"},
            {"name": "period", "type": "str", "description": "Reporting period"},
        ],
    },
    "list_filings": {
        "category": "filings",
        "description": "List a CN company's CNINFO disclosures",
        "source": CN_REPORT_SOURCE,
        "parameters": [{"name": "ticker", "type": "str", "required": True}],
        "columns": [
            {"name": "announcement_id", "type": "str", "description": "CNINFO announcement id"},
            {"name": "title", "type": "str", "description": "Announcement title"},
            {"name": "date", "type": "str", "description": "Announcement date"},
        ],
    },
    "get_section": {
        "category": "report",
        "description": "Resolve a filing and extract one named section",
        "source": CN_REPORT_SOURCE,
        "parameters": [
            {"name": "ticker", "type": "str", "required": True},
            {"name": "selector", "type": "str", "required": True},
        ],
        "columns": [
            {"name": "title", "type": "str", "description": "Section title"},
            {"name": "text", "type": "str", "description": "Section body text"},
        ],
    },
    "list_indicators": {
        "category": "indicators",
        "description": "Browse the indicator rule set (data-driven by llm_rules/script_rules)",
        "source": CN_REPORT_SOURCE,
        "parameters": [{"name": "module", "type": "str", "required": False}],
        "columns": [
            {"name": "indicator", "type": "str", "description": "Indicator name"},
            {"name": "document_type", "type": "str", "description": "Report type"},
            {"name": "module", "type": "str", "description": "Statement module"},
        ],
    },
}

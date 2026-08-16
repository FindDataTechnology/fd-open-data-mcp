"""Curated seed of cnstats (Chinese NBS macro) commands.

cnstats is **not** a separate library — the 8 curated NBS macro indicators
(CPI, PMI, industrial output, fixed-asset investment, retail sales, GDP,
trade balance, money supply) are backed by **akshare** macro functions
(``macro_china_cpi_yearly`` etc., per the DAAS dispatch.py parity target).
It is **keyless** (no env var, no API key — akshare fetches the NBS-published
macro series directly).

The akshare macro functions return a ``pandas.DataFrame`` with a 日期 (date)
axis and Chinese-named value columns; this seed records those column shapes
verbatim from ``fd_world/sources/cnstats_source.py`` CNSTATS_FUNCTIONS. The
cnstats command names here are **unprefixed** (``cpi`` not ``cnstats_cpi``) —
the adapter/runner convention — and the adapter maps each to its akshare
function via the ``_CnstatsBase.MAPPING`` dict.

This seed lives inside ``fd-open-data-mcp`` so catalog import works
**without** the ``data`` extra installed — only upstream fetch requires the
``akshare`` package (already a hard dep via the akshare adapter).
"""
from __future__ import annotations

CNSTATS_SOURCE = "https://data.stats.gov.cn/"

# ---------------------------------------------------------------------------
# Column shapes (verbatim from fd_world/sources/cnstats_source.py)
# ---------------------------------------------------------------------------

_CPI_COLUMNS = [
    {"name": "日期", "type": "datetime64", "description": "Date"},
    {"name": "全国同比", "type": "float64", "description": "National YoY %"},
    {"name": "全国环比", "type": "float64", "description": "National MoM %"},
    {"name": "城市同比", "type": "float64", "description": "Urban YoY %"},
    {"name": "农村同比", "type": "float64", "description": "Rural YoY %"},
]

_PMI_COLUMNS = [
    {"name": "日期", "type": "datetime64", "description": "Date"},
    {"name": "制造业", "type": "float64", "description": "Manufacturing PMI"},
    {"name": "非制造业", "type": "float64", "description": "Non-manufacturing PMI"},
    {"name": "综合", "type": "float64", "description": "Composite PMI"},
]

_INDUSTRIAL_OUTPUT_COLUMNS = [
    {"name": "日期", "type": "datetime64", "description": "Date"},
    {"name": "工业增加值同比", "type": "float64", "description": "Industrial output YoY %"},
    {"name": "累计同比", "type": "float64", "description": "Cumulative YoY %"},
]

_FIXED_ASSET_INVESTMENT_COLUMNS = [
    {"name": "日期", "type": "datetime64", "description": "Date"},
    {"name": "固定资产投资同比", "type": "float64", "description": "FAI YoY %"},
    {"name": "民间投资同比", "type": "float64", "description": "Private investment YoY %"},
]

_RETAIL_SALES_COLUMNS = [
    {"name": "日期", "type": "datetime64", "description": "Date"},
    {"name": "社会消费品零售总额同比", "type": "float64",
     "description": "Retail sales YoY %"},
    {"name": "限额以上同比", "type": "float64",
     "description": "Above-designated-size YoY %"},
]

_GDP_QUARTERLY_COLUMNS = [
    {"name": "日期", "type": "datetime64", "description": "Quarter"},
    {"name": "GDP同比", "type": "float64", "description": "GDP YoY %"},
    {"name": "GDP环比", "type": "float64", "description": "GDP QoQ %"},
    {"name": "第一产业同比", "type": "float64", "description": "Primary industry YoY %"},
    {"name": "第二产业同比", "type": "float64", "description": "Secondary industry YoY %"},
    {"name": "第三产业同比", "type": "float64", "description": "Tertiary industry YoY %"},
]

_TRADE_BALANCE_COLUMNS = [
    {"name": "日期", "type": "datetime64", "description": "Date"},
    {"name": "出口金额", "type": "float64", "description": "Export value (USD)"},
    {"name": "进口金额", "type": "float64", "description": "Import value (USD)"},
    {"name": "贸易差额", "type": "float64", "description": "Trade balance (USD)"},
]

_MONEY_SUPPLY_COLUMNS = [
    {"name": "日期", "type": "datetime64", "description": "Date"},
    {"name": "M0", "type": "float64", "description": "M0 money supply"},
    {"name": "M1", "type": "float64", "description": "M1 money supply"},
    {"name": "M2", "type": "float64", "description": "M2 money supply"},
]

# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

REGISTRY: dict[str, dict] = {
    "cpi": {
        "category": "macro",
        "description": "Monthly CPI year-over-year change for China",
        "source": CNSTATS_SOURCE,
        "parameters": [],
        "columns": _CPI_COLUMNS,
    },
    "pmi": {
        "category": "macro",
        "description": "Monthly manufacturing and non-manufacturing PMI for China",
        "source": CNSTATS_SOURCE,
        "parameters": [],
        "columns": _PMI_COLUMNS,
    },
    "industrial_output": {
        "category": "industry",
        "description": "Monthly industrial added value growth rate for China",
        "source": CNSTATS_SOURCE,
        "parameters": [],
        "columns": _INDUSTRIAL_OUTPUT_COLUMNS,
    },
    "fixed_asset_investment": {
        "category": "investment",
        "description": "Monthly fixed asset investment growth for China",
        "source": CNSTATS_SOURCE,
        "parameters": [],
        "columns": _FIXED_ASSET_INVESTMENT_COLUMNS,
    },
    "retail_sales": {
        "category": "consumption",
        "description": "Monthly total retail sales of consumer goods growth rate",
        "source": CNSTATS_SOURCE,
        "parameters": [],
        "columns": _RETAIL_SALES_COLUMNS,
    },
    "gdp_quarterly": {
        "category": "macro",
        "description": "Quarterly GDP growth rates for China",
        "source": CNSTATS_SOURCE,
        "parameters": [],
        "columns": _GDP_QUARTERLY_COLUMNS,
    },
    "trade_balance": {
        "category": "trade",
        "description": "Monthly import/export data for China",
        "source": CNSTATS_SOURCE,
        "parameters": [],
        "columns": _TRADE_BALANCE_COLUMNS,
    },
    "money_supply": {
        "category": "finance",
        "description": "Monthly M0, M1, M2 money supply data for China",
        "source": CNSTATS_SOURCE,
        "parameters": [],
        "columns": _MONEY_SUPPLY_COLUMNS,
    },
}

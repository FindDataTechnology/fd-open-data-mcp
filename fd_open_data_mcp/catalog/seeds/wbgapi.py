"""Curated seed of wbgapi (World Bank data API) commands.

wbgapi (PyPI dist ``wbgapi``, imported as ``wbgapi``) gives modern access to
the World Bank data API. This seed defines the key commands the ontology
dispatches to. Indicator codes (``NY.GDP.MKTP.CD``, ``SP.POP.TOTL``, ...) are
modeled as the ``get_indicator_data`` function's *columns* - each binds to a
concept, and the runner passes the column name as the ``indicator`` parameter.
"""
from __future__ import annotations

WBGAPI_SOURCE = "https://data.worldbank.org/"

_ECONOMY_PARAM = {
    "name": "economy", "type": "str", "required": True,
    "description": "iso3 economy code, e.g. 'CHN', 'USA'",
}
_DATE_PARAM = {
    "name": "date", "type": "str", "required": True,
    "description": "year, e.g. '2023'",
}
_INDICATOR_PARAM = {
    "name": "indicator", "type": "str", "required": True,
    "description": "World Bank indicator code (injected from the concept binding's column name)",
}

# Curated common World Bank indicators -> each becomes a `columns` row.
_INDICATOR_COLUMNS = [
    {"name": "NY.GDP.MKTP.CD", "type": "float", "description": "GDP (current US$)"},
    {"name": "NY.GDP.PCAP.CD", "type": "float", "description": "GDP per capita (current US$)"},
    {"name": "NY.GDP.MKTP.KD.ZG", "type": "float", "description": "GDP growth (annual %)"},
    {"name": "SP.POP.TOTL", "type": "float", "description": "Population, total"},
    {"name": "EN.ATM.CO2E.KT", "type": "float", "description": "CO2 emissions (kt)"},
    {"name": "EN.ATM.CO2E.PC", "type": "float", "description": "CO2 emissions per capita"},
    {"name": "FP.CPI.TOTL.ZG", "type": "float", "description": "Inflation, consumer prices (annual %)"},
    {"name": "SL.UEM.TOTL.ZS", "type": "float", "description": "Unemployment, total (% of labor force)"},
]

REGISTRY: dict[str, dict] = {
    "get_indicator_data": {
        "category": "macro",
        "description": "Fetch one World Bank indicator for an economy (iso3) and year",
        "source": WBGAPI_SOURCE,
        "parameters": [_ECONOMY_PARAM, _DATE_PARAM, _INDICATOR_PARAM],
        "columns": _INDICATOR_COLUMNS,
    },
    "list_indicators": {
        "category": "meta",
        "description": "Search/list World Bank indicator series",
        "source": WBGAPI_SOURCE,
        "parameters": [{"name": "q", "type": "str", "required": False, "description": "search keyword"}],
        "columns": [
            {"name": "id", "type": "str", "description": "indicator code"},
            {"name": "value", "type": "str", "description": "indicator name"},
        ],
    },
    "list_economies": {
        "category": "meta",
        "description": "List World Bank economies (countries + aggregates)",
        "source": WBGAPI_SOURCE,
        "parameters": [],
        "columns": [
            {"name": "id", "type": "str", "description": "iso3 economy code"},
            {"name": "name", "type": "str", "description": "economy name"},
            {"name": "region", "type": "str", "description": "World Bank region"},
            {"name": "income_level", "type": "str", "description": "income group"},
        ],
    },
    "get_series_metadata": {
        "category": "meta",
        "description": "Metadata for one World Bank indicator series",
        "source": WBGAPI_SOURCE,
        "parameters": [{"name": "indicator", "type": "str", "required": True, "description": "indicator code"}],
        "columns": [
            {"name": "id", "type": "str", "description": "indicator code"},
            {"name": "value", "type": "str", "description": "indicator name"},
            {"name": "unit", "type": "str", "description": "unit of measure"},
            {"name": "source", "type": "str", "description": "source dataset"},
        ],
    },
}

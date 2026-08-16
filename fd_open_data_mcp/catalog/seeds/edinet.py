"""Curated seed of edinet-tools (``edinet_tools``) commands.

edinet-tools (PyPI dist ``edinet-tools``) imports as ``edinet_tools``; its API
is object-oriented around ``Entity`` (like edgar's ``Company``):

  - ``edinet_tools.entity(code)`` resolves an entity by EDINET code
    ('E02144'), JP ticker ('7203' / '7203.T'), corporate number, or name.
    KEYLESS - reads the bundled FSA registry CSVs.
  - ``entity.documents(doc_type="120", days=365)`` -> ``list[Document]`` is
    the only path requiring ``EDINET_API_KEY``.

This seed defines the key commands the ontology dispatches to. It lives inside
``fd-open-data-mcp`` so catalog import works **without** the ``data`` extra
installed - only upstream introspection + fetch require the ``edinet-tools``
package.
"""
from __future__ import annotations

EDINET_SOURCE = "https://disclosure.edinet-fsa.go.jp/"

_CODE_PARAM = {
    "name": "code",
    "type": "str",
    "required": True,
    "description": "EDINET code ('E02144'), JP ticker ('7203' / '7203.T'), corporate number, or entity name",
}

# Filing-metadata columns materialised from Document objects by the adapter
# (the date axis is filing_datetime; see adapters/edinet.py _DOC_COLUMNS).
_DOCUMENT_COLUMNS = [
    {"name": "doc_id", "type": "str", "description": "EDINET document id, e.g. 'S100ABC'"},
    {"name": "doc_type_code", "type": "str", "description": "Document type code, e.g. '120' (Securities Report)"},
    {"name": "doc_type_name", "type": "str", "description": "Document type name"},
    {"name": "filer_edinet_code", "type": "str", "description": "Filer's EDINET code"},
    {"name": "filer_name", "type": "str", "description": "Filer name"},
    {"name": "filing_datetime", "type": "datetime", "description": "Filing datetime (JST)"},
    {"name": "securities_code", "type": "str", "description": "Listed securities code"},
    {"name": "period_start", "type": "str", "description": "Reporting period start"},
    {"name": "period_end", "type": "str", "description": "Reporting period end"},
    {"name": "doc_description", "type": "str", "description": "Document description"},
]

REGISTRY: dict[str, dict] = {
    "entity_documents": {
        "category": "filings",
        "description": "EDINET filings for an entity, optionally filtered by doc_type; looks back N days from today (JST)",
        "source": EDINET_SOURCE,
        "parameters": [
            _CODE_PARAM,
            {"name": "doc_type", "type": "str", "required": False,
             "description": "Document type code, e.g. '120' (Securities Report), '350' (Large Shareholding)"},
            {"name": "days", "type": "int", "required": False,
             "description": "Lookback window in days (default 365)"},
        ],
        "columns": _DOCUMENT_COLUMNS,
    },
    "documents": {
        "category": "filings",
        "description": "All EDINET filings on a single date (requires EDINET_API_KEY)",
        "source": EDINET_SOURCE,
        "parameters": [
            {"name": "date", "type": "str", "required": True,
             "description": "Filing date YYYY-MM-DD"},
            {"name": "doc_type", "type": "str", "required": False,
             "description": "Document type code"},
        ],
        "columns": _DOCUMENT_COLUMNS,
    },
    "entity": {
        "category": "entities",
        "description": "Resolve an entity by EDINET code, ticker, corporate number, or name (keyless)",
        "source": EDINET_SOURCE,
        "parameters": [_CODE_PARAM],
        "columns": [
            {"name": "edinet_code", "type": "str", "description": "EDINET code"},
            {"name": "name", "type": "str", "description": "Entity name"},
            {"name": "ticker", "type": "str", "description": "Listed ticker"},
        ],
    },
    "search_entities": {
        "category": "entities",
        "description": "Search entities by name substring (keyless)",
        "source": EDINET_SOURCE,
        "parameters": [
            {"name": "query", "type": "str", "required": True,
             "description": "Name substring, e.g. 'トヨタ'"},
            {"name": "limit", "type": "int", "required": False,
             "description": "Max results (default 10)"},
        ],
        "columns": [
            {"name": "edinet_code", "type": "str", "description": "EDINET code"},
            {"name": "name", "type": "str", "description": "Entity name"},
        ],
    },
    "fetch_and_parse": {
        "category": "filings",
        "description": "Fetch + parse a filing by document id into a typed report (requires EDINET_API_KEY)",
        "source": EDINET_SOURCE,
        "parameters": [
            {"name": "doc_id", "type": "str", "required": True,
             "description": "EDINET document id"},
            {"name": "doc_type", "type": "str", "required": True,
             "description": "Document type code, e.g. '120'"},
        ],
        "columns": [
            {"name": "net_sales", "type": "float", "description": "Net sales (Securities Report)"},
            {"name": "operating_cash_flow", "type": "float", "description": "Cash from operating activities"},
            {"name": "roe", "type": "float", "description": "Return on equity"},
        ],
    },
    "supported_doc_types": {
        "category": "meta",
        "description": "List the EDINET document type codes (keyless)",
        "source": EDINET_SOURCE,
        "parameters": [],
        "columns": [
            {"name": "code", "type": "str", "description": "Document type code"},
            {"name": "name", "type": "str", "description": "Document type name"},
        ],
    },
}

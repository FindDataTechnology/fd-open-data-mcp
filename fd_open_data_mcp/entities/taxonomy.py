"""Entity taxonomy lookup against fd-entities-indicators (read-only).

Entities are NOT copied into the ontology DB - they live in
``entities_indicators.db``. This module looks up an entity by type + code and
returns its row (including id), so ``entity_source_identifiers`` can reference
it by ``(entity_type, entity_id)``.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Optional

from fd_open_data_mcp.catalog.providers import finddata_root


def default_entities_db() -> str:
    return str(finddata_root().joinpath("fd-entities-indicators", "entities_indicators.db"))


# Canonical entity_type vocabulary. Aligned with fd-entities-indicators
# (countries/cities/symbols/sw_industries) plus `organization` (logical-only -
# no taxonomy table). `entity_type` is a free string; this is the documented set.
ENTITY_TYPES: tuple[str, ...] = (
    "country", "city", "stock", "fund", "bond", "index",
    "future", "crypto", "organization", "industry",
)

# entity_type -> (table, code_column)
ENTITY_TABLES: dict[str, tuple[str, str]] = {
    "country": ("countries", "iso_code"),
    "city": ("cities", "id"),
    "stock": ("symbols", "code"),
    "industry": ("sw_industries", "code"),
}


def list_entities(entity_type: str, db_path: Optional[str] = None) -> list[dict]:
    """Return all entities of a type as dicts."""
    path = db_path or default_entities_db()
    if entity_type not in ENTITY_TABLES or not Path(path).exists():
        return []
    table, _code_col = ENTITY_TABLES[entity_type]
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(f"SELECT * FROM {table}").fetchall()
        return [dict(r) for r in rows]
    except Exception:  # noqa: BLE001
        return []
    finally:
        conn.close()


def find_entity(entity_type: str, code: str, db_path: Optional[str] = None) -> Optional[dict]:
    """Look up one entity by its code column. Returns the row dict (with id) or None."""
    path = db_path or default_entities_db()
    if entity_type not in ENTITY_TABLES or not Path(path).exists():
        return None
    table, code_col = ENTITY_TABLES[entity_type]
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(f"SELECT * FROM {table} WHERE {code_col} = ?", (code,)).fetchone()
        return dict(row) if row else None
    except Exception:  # noqa: BLE001
        return None
    finally:
        conn.close()

"""Idempotent schema bootstrap for fd-open-data-mcp.

Creates all ontology tables (with FKs + unique indexes) if absent. Safe to
re-run - existing tables are left untouched (CREATE TABLE IF NOT EXISTS via
SQLAlchemy create_all). Also adds new columns to existing tables that
``create_all`` cannot alter (add-source-proxy-health: fetch_log.proxy_id +
fetch_log.classification) via idempotent ADD COLUMN.

Usage:
    python -m fd_open_data_mcp.migrate
    fd-open-data-mcp migrate
"""
from __future__ import annotations

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

from fd_open_data_mcp.db import get_database
from fd_open_data_mcp.models import Base


# Columns create_all cannot add to an existing table; migrate them idempotently.
# (dialect-aware: ADD COLUMN IF NOT EXISTS on postgres; check inspect on sqlite)
_ALTER_COLUMNS = {
    "fetch_log": [("proxy_id", "INTEGER"), ("classification", "VARCHAR(16)")],
}


def _add_missing_columns(engine: Engine) -> list[str]:
    """Add columns listed in _ALTER_COLUMNS if absent. Returns the list added."""
    insp = inspect(engine)
    if "fetch_log" not in insp.get_table_names():
        return []
    added: list[str] = []
    for table, cols in _ALTER_COLUMNS.items():
        existing = {c["name"] for c in insp.get_columns(table)}
        for col_name, col_type in cols:
            if col_name in existing:
                continue
            dialect = engine.dialect.name
            if dialect == "sqlite":
                stmt = f'ALTER TABLE {table} ADD COLUMN "{col_name}" {col_type}'
            else:
                stmt = f'ALTER TABLE {table} ADD COLUMN IF NOT EXISTS "{col_name}" {col_type}'
            with engine.begin() as conn:
                conn.execute(text(stmt))
            added.append(f"{table}.{col_name}")
    return added


def migrate() -> dict:
    """Create all tables if absent, add new columns to existing tables, return summary."""
    db = get_database()
    Base.metadata.create_all(db.engine)
    added_columns = _add_missing_columns(db.engine)
    insp = inspect(db.engine)
    tables = sorted(insp.get_table_names())
    return {
        "database_url": db.database_url,
        "tables": tables,
        "table_count": len(tables),
        "added_columns": added_columns,
    }


if __name__ == "__main__":
    result = migrate()
    print(f"Initialized {result['table_count']} tables at {result['database_url']}:")
    for name in result["tables"]:
        print(f"  - {name}")
    if result["added_columns"]:
        print("Added columns:")
        for c in result["added_columns"]:
            print(f"  + {c}")

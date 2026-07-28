"""Idempotent schema bootstrap for fd-open-data-mcp.

Creates all ontology tables (with FKs + unique indexes) if absent. Safe to
re-run - existing tables are left untouched (CREATE TABLE IF NOT EXISTS via
SQLAlchemy create_all).

Usage:
    python -m fd_open_data_mcp.migrate
    fd-open-data-mcp migrate
"""
from __future__ import annotations

from sqlalchemy import inspect

from fd_open_data_mcp.db import get_database
from fd_open_data_mcp.models import Base


def migrate() -> dict:
    """Create all tables if absent and return a summary."""
    db = get_database()
    Base.metadata.create_all(db.engine)
    insp = inspect(db.engine)
    tables = sorted(insp.get_table_names())
    return {"database_url": db.database_url, "tables": tables, "table_count": len(tables)}


if __name__ == "__main__":
    result = migrate()
    print(f"Initialized {result['table_count']} tables at {result['database_url']}:")
    for name in result["tables"]:
        print(f"  - {name}")

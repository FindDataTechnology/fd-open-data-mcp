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
    # add-multi-cluster-master-db: per-cluster identity for runs + direct egress.
    # Nullable FKs (SET NULL on cluster delete) so legacy rows survive.
    "policy_runs": [("cluster_id", "INTEGER")],
    "proxies": [("cluster_id", "INTEGER")],
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


# astock_daily column -> (concept code, unit) for System-B stock concepts.
# The legacy astock_daily table holds 14.2M rows of A-share OHLCV (adjust='qfq',
# period='daily') and is the bulk-ingest source for stocks missing from
# semantic_observations (fix-stock-semantic-retrieval, task 7.1).
ASTOCK_CONCEPT_MAP = {
    "open": ("price.open", "currency"),
    "close": ("price.close", "currency"),
    "high": ("price.high", "currency"),
    "low": ("price.low", "currency"),
    "volume": ("price.volume", "shares"),
    "amount": ("price.amount", "currency"),
}


def _stock_concept_ids(session) -> dict[str, int]:
    """Resolve System-B stock concept IDs by code (canonical, non-deprecated).

    Duplicate stock concepts exist (e.g. id 234 ``price.close`` with name_zh and
    id 269 ``price.close`` with null name_zh); take the lowest-id non-deprecated
    concept per code, which is the canonical 233-238 set.
    """
    from fd_open_data_mcp.models import Concept
    rows = (
        session.query(Concept)
        .filter(
            Concept.code.in_(list({c for c, _ in ASTOCK_CONCEPT_MAP.values()})),
            Concept.entity_type == "stock",
            Concept.deprecated.is_(False),
        )
        .all()
    )
    by_code: dict[str, int] = {}
    for c in sorted(rows, key=lambda r: r.id):
        by_code.setdefault(c.code, c.id)
    return by_code


def migrate_astock_daily(session, symbols: list[str] | None = None) -> dict:
    """Bulk-migrate astock_daily OHLCV into semantic_observations (System-B concepts).

    Idempotent via ``ON CONFLICT (concept_id, entity_type, entity_id, date, granularity)
    DO NOTHING`` (granularity = 'day' for daily OHLCV). If ``symbols`` is given,
    migrate only those symbols (used for testing / targeted backfill); otherwise
    migrate all astock_daily rows for symbols that map to a ``stock`` entity.
    """
    code_to_id = _stock_concept_ids(session)
    expected = {c for c, _ in ASTOCK_CONCEPT_MAP.values()}
    missing = expected - set(code_to_id)
    if missing:
        raise ValueError(f"missing canonical stock concepts: {sorted(missing)}")

    sym_filter = "AND a.symbol = ANY(:symbols)" if symbols else ""
    params: dict = {"symbols": symbols} if symbols else {}
    params["et"] = "stock"
    params["src"] = "astock_daily"

    results = {}
    for col, (code, unit) in ASTOCK_CONCEPT_MAP.items():
        cid = code_to_id[code]
        # astock_daily is daily OHLCV -> granularity 'day' (fix-observation-time-granularity)
        sql = f"""
            INSERT INTO semantic_observations
                (concept_id, entity_type, entity_id, date, granularity, value, unit, source_used, fetched_at)
            SELECT :cid, :et, e.id, a.trade_date::text, 'day', a.{col}::text, :unit, :src, now()
            FROM astock_daily a
            JOIN entities e ON e.entity_type = :et AND e.code = a.symbol
            WHERE a.{col} IS NOT NULL
            {sym_filter}
            ON CONFLICT (concept_id, entity_type, entity_id, date, granularity) DO NOTHING
        """
        p = {**params, "cid": cid, "unit": unit}
        res = session.execute(text(sql), p)
        results[col] = res.rowcount
        session.commit()
    return {"concept_map": code_to_id, "inserted_by_column": results}

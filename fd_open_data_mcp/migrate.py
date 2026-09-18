"""Schema migrations entry point for fd-open-data-mcp.

The ``migrate`` CLI is the deploy-time migration stage: it delegates to
``fd_open_data_mcp.db.migrate_stage`` — the same advisory-locked
``alembic upgrade head`` the Deployment's initContainer runs — and nothing
else. The parallel mechanisms it used to carry (``create_all`` bootstrap,
idempotent ``_ALTER_COLUMNS``, the sem-obs key swap) were retired into the
Alembic chain by the db-schema-lifecycle change: the baseline revision
``0001_schema_baseline`` reproduces the verified schema snapshot and
``0002_sem_obs_source_aware_key`` carries the swap. The manual SQL runbooks
live on as history in ``docs/migrations-archive/``.

Usage:
    python -m fd_open_data_mcp.migrate
    fd-open-data-mcp migrate

PostgreSQL runs the migration chain. SQLite (local dev) has no chain — the
startup gate exempts it and the baseline revision is PostgreSQL DDL — so its
databases are bootstrapped from the models with create_all, which is exactly
how the verified baseline snapshot was produced.
"""
from __future__ import annotations

import os

from sqlalchemy import create_engine, inspect


def migrate() -> dict:
    """Upgrade the database to the code's schema, return a summary.

    PostgreSQL: delegates to the deploy-time migration stage
    (advisory-locked ``alembic upgrade head``). SQLite: bootstraps from the
    models. Deliberately does not touch the application ``Database``
    singleton: its startup gate would refuse the very PostgreSQL databases
    this CLI exists to upgrade.
    """
    from fd_open_data_mcp.db import Database

    database_url = os.environ.get("FD_OPEN_DATA_MCP_DATABASE_URL") or Database().database_url

    if database_url.startswith("sqlite"):
        from fd_open_data_mcp.models import Base

        engine = create_engine(database_url)
        try:
            Base.metadata.create_all(engine)
            tables = sorted(inspect(engine).get_table_names())
        finally:
            engine.dispose()
        return {
            "database_url": database_url,
            "tables": tables,
            "table_count": len(tables),
        }

    from fd_open_data_mcp.db.migrate_stage import run as run_migration_stage

    run_migration_stage()

    engine = create_engine(database_url)
    try:
        tables = sorted(inspect(engine).get_table_names())
    finally:
        engine.dispose()
    return {
        "database_url": database_url,
        "tables": tables,
        "table_count": len(tables),
    }


if __name__ == "__main__":
    result = migrate()
    print(f"Migrated to head: {result['table_count']} tables at {result['database_url']}")


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

    Idempotent via ``ON CONFLICT (concept_id, entity_type, entity_id, date, granularity,
    source_used) DO NOTHING`` (granularity = 'day' for daily OHLCV; the astock_daily
    backfill owns its own ``astock_daily`` source row per point). If ``symbols`` is given,
    migrate only those symbols (used for testing / targeted backfill); otherwise
    migrate all astock_daily rows for symbols that map to a ``stock`` entity.
    """
    code_to_id = _stock_concept_ids(session)
    expected = {c for c, _ in ASTOCK_CONCEPT_MAP.values()}
    missing = expected - set(code_to_id)
    if missing:
        raise ValueError(f"missing canonical stock concepts: {sorted(missing)}")

    from sqlalchemy import text

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
            ON CONFLICT (concept_id, entity_type, entity_id, date, granularity, source_used) DO NOTHING
        """
        p = {**params, "cid": cid, "unit": unit}
        res = session.execute(text(sql), p)
        results[col] = res.rowcount
        session.commit()
    return {"concept_map": code_to_id, "inserted_by_column": results}

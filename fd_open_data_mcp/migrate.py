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
    # add-semantic-vocabulary-core: the Variable's concept-family reference.
    "concepts": [("concept_code", "VARCHAR(128)")],
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


# --- uq_sem_obs source-aware key swap (add-multi-source-observations) --------
# The observation unique key gains source_used. The relaxed key is a strict
# superset of the old one, so every existing row satisfies it by construction
# — the swap cannot fail on data. Postgres swaps online (CONCURRENTLY index
# build, then a dictionary-only lock constraint swap); SQLite rebuilds the
# table (its auto-indexes cannot be dropped in place).

_SEM_OBS_SWAP_KEY_COLS = ("concept_id", "entity_type", "entity_id", "date",
                          "granularity", "source_used")
_ADVISORY_LOCK_KEY = "fd_mcp_uq_sem_obs_swap"


def _sem_obs_key_is_source_aware(engine: Engine) -> bool:
    """True when a unique index/constraint over the 6-col key already exists on
    semantic_observations. Both forms are checked: on Postgres the swapped key
    is a constraint-backed index (get_indexes); on SQLite a table-level UNIQUE
    constraint is an auto-index the dialect only reports via
    get_unique_constraints."""
    insp = inspect(engine)
    if "semantic_observations" not in insp.get_table_names():
        return False  # nothing to migrate; create_all builds the new key
    key_set = set(_SEM_OBS_SWAP_KEY_COLS)
    for idx in insp.get_indexes("semantic_observations"):
        if idx.get("unique") and set(idx.get("column_names") or ()) == key_set:
            return True
    for uq in insp.get_unique_constraints("semantic_observations"):
        if set(uq.get("column_names") or ()) == key_set:
            return True
    return False


def _swap_sem_obs_key_postgres(engine: Engine) -> str:
    """Online swap on Postgres: CONCURRENTLY-build the 6-col unique index,
    then attach it as the uq_sem_obs constraint (dictionary-lock only)."""
    swap_sql = f"""
        CREATE UNIQUE INDEX CONCURRENTLY uq_sem_obs_src
            ON semantic_observations ({', '.join(_SEM_OBS_SWAP_KEY_COLS)});
        DROP INDEX IF EXISTS uq_sem_obs;
        ALTER TABLE semantic_observations DROP CONSTRAINT IF EXISTS uq_sem_obs;
        ALTER TABLE semantic_observations
            ADD CONSTRAINT uq_sem_obs UNIQUE USING INDEX uq_sem_obs_src;
    """
    # CONCURRENTLY cannot run inside a transaction block: use an autocommit
    # connection and execute statement-by-statement. The advisory lock keeps
    # two migrators (or migrate + the 007 runbook script) from racing.
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        conn.exec_driver_sql(f"SELECT pg_advisory_lock(hashtext('{_ADVISORY_LOCK_KEY}'))")
        try:
            # A previous failed build may have left an INVALID uq_sem_obs_src
            # (CONCURRENTLY leaves the index behind on error); drop it so the
            # rebuild below actually happens.
            invalid = conn.exec_driver_sql(
                "SELECT 1 FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
                "WHERE c.relname = 'uq_sem_obs_src' AND NOT i.indisvalid"
            ).scalar()
            if invalid:
                conn.exec_driver_sql("DROP INDEX CONCURRENTLY IF EXISTS uq_sem_obs_src")
            for stmt in filter(None, (s.strip() for s in swap_sql.split(";"))):
                if "CREATE UNIQUE INDEX CONCURRENTLY" in stmt:
                    exists = conn.exec_driver_sql(
                        "SELECT 1 FROM pg_class WHERE relname = 'uq_sem_obs_src'"
                    ).scalar()
                    if exists:
                        continue  # valid index from an interrupted prior run
                conn.exec_driver_sql(stmt)
        finally:
            conn.exec_driver_sql(
                f"SELECT pg_advisory_unlock(hashtext('{_ADVISORY_LOCK_KEY}'))")
    return "uq_sem_obs swapped to source-aware key (concurrent build)"


def _swap_sem_obs_key_sqlite(engine: Engine) -> str:
    """SQLite rebuild: the old table-level UNIQUE constraint backs an auto-index
    that cannot be dropped, so recreate the table from the current model DDL and
    copy rows across (the relaxed key admits every existing row)."""
    from sqlalchemy.schema import CreateTable, CreateIndex
    from fd_open_data_mcp.models import SemanticObservation

    tbl = SemanticObservation.__table__
    cols = ", ".join(f'"{c.name}"' for c in tbl.columns)
    with engine.begin() as conn:
        conn.exec_driver_sql('ALTER TABLE semantic_observations RENAME TO semantic_observations_old_uq')
        try:
            ddl = str(CreateTable(tbl).compile(engine))
            conn.exec_driver_sql(ddl)
            for idx in tbl.indexes:
                conn.exec_driver_sql(str(CreateIndex(idx).compile(engine)))
            conn.exec_driver_sql(
                f'INSERT INTO semantic_observations ({cols}) SELECT {cols} '
                'FROM semantic_observations_old_uq')
            conn.exec_driver_sql('DROP TABLE semantic_observations_old_uq')
        except Exception:
            # Put the original table back so a failed migrate is recoverable.
            conn.exec_driver_sql('DROP TABLE IF EXISTS semantic_observations')
            conn.exec_driver_sql(
                'ALTER TABLE semantic_observations_old_uq RENAME TO semantic_observations')
            raise
    return "uq_sem_obs rebuilt source-aware (sqlite table rebuild)"


def _swap_sem_obs_key(engine: Engine) -> list[str]:
    if _sem_obs_key_is_source_aware(engine):
        return []
    if engine.dialect.name == "sqlite":
        return [_swap_sem_obs_key_sqlite(engine)]
    return [_swap_sem_obs_key_postgres(engine)]


def migrate() -> dict:
    """Create all tables if absent, add new columns to existing tables, swap the
    observation unique key to its source-aware form, return summary."""
    db = get_database()
    Base.metadata.create_all(db.engine)
    added_columns = _add_missing_columns(db.engine)
    key_swaps = _swap_sem_obs_key(db.engine)
    insp = inspect(db.engine)
    tables = sorted(insp.get_table_names())
    return {
        "database_url": db.database_url,
        "tables": tables,
        "table_count": len(tables),
        "added_columns": added_columns,
        "key_swaps": key_swaps,
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

    Idempotent via ``ON CONFLICT (concept_id, entity_type, entity_id, date, granularity,
    source_used) DO NOTHING`` (granularity = 'day' for daily OHLCV; the astock_daily
    backfill owns its own ``astock_daily`` source row per point). If ``symbols`` is given,
    migrate only those symbols (used for testing / targeted backfill); otherwise
    migrate all astock_daily rows for symbols that map to a ``stock`` entity.
    """
    from sqlalchemy import text

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
            ON CONFLICT (concept_id, entity_type, entity_id, date, granularity, source_used) DO NOTHING
        """
        p = {**params, "cid": cid, "unit": unit}
        res = session.execute(text(sql), p)
        results[col] = res.rowcount
        session.commit()
    return {"concept_map": code_to_id, "inserted_by_column": results}

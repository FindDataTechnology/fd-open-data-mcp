"""Mirror the fund/person ontology + crawl policies from the live DB to the
in-cluster fd-open-pg (which the reconciler + crawl Jobs actually use).

The cluster cannot reach 192.168.1.4 (LAN), so the reconciler's DB is the
in-cluster fd-open-pg. This copies the fund/person concepts, entities,
identifiers, relationships, bindings and crawl_policies/policy_runs (creating
the migration-002 tables if absent). Idempotent — safe to re-run.

Usage:
    python scripts/mirror_fund_to_cluster.py \
      --source 'postgresql://admin:admin123@192.168.1.4:5433/postgres' \
      --target 'postgresql+psycopg2://postgres:admin123@127.0.0.1:55432/postgres'
"""
from __future__ import annotations

import argparse
import json
import logging

from sqlalchemy import create_engine, text

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("mirror")

MIGRATION_002 = """
CREATE TABLE IF NOT EXISTS crawl_policies (
    id SERIAL PRIMARY KEY,
    name VARCHAR(128) NOT NULL UNIQUE,
    enabled BOOLEAN NOT NULL DEFAULT true,
    concept_ids JSONB NOT NULL,
    entity_type VARCHAR(32) NOT NULL,
    entity_ids JSONB,
    date_policy JSONB NOT NULL,
    frequency VARCHAR(32) NOT NULL DEFAULT 'daily',
    mode VARCHAR(16) NOT NULL DEFAULT 'per_date',
    source_filter JSONB,
    force BOOLEAN NOT NULL DEFAULT false,
    cron_expr VARCHAR(128) NOT NULL,
    timezone VARCHAR(64) NOT NULL DEFAULT 'UTC',
    last_run_at TIMESTAMP,
    created_at TIMESTAMP
);
CREATE TABLE IF NOT EXISTS policy_runs (
    id SERIAL PRIMARY KEY,
    policy_id INTEGER NOT NULL REFERENCES crawl_policies(id) ON DELETE CASCADE,
    status VARCHAR(32) NOT NULL DEFAULT 'running',
    plan_json JSONB,
    job_ref VARCHAR(255),
    started_at TIMESTAMP NOT NULL,
    finished_at TIMESTAMP,
    detail TEXT
);
"""

# table -> (where-clause on source, conflict-columns)
COPIES = [
    # concepts: only fund/person rows (stock/macro rows already present + same ids)
    ("concepts", "entity_type IN ('fund','person')", ["id"]),
    # entities: only fund/person
    ("entities", "entity_type IN ('fund','person')", ["id"]),
    # identifiers for those entity types
    ("entity_source_identifiers", "entity_type IN ('fund','person')", ["entity_type", "entity_id", "source"]),
    # fund-referenced functions+columns before bindings (FK order)
    ("functions",
     "id IN (SELECT DISTINCT function_id FROM columns c JOIN concept_bindings b "
     "ON b.column_id=c.id WHERE b.concept_id IN "
     "(SELECT id FROM concepts WHERE entity_type IN ('fund','person')))",
     ["source_id", "command"]),
    ("columns",
     "id IN (SELECT column_id FROM concept_bindings WHERE concept_id IN "
     "(SELECT id FROM concepts WHERE entity_type IN ('fund','person')))",
     ["function_id", "name"]),
    # bindings pointing at fund/person concepts (subselect keeps ids aligned)
    ("concept_bindings",
     "concept_id IN (SELECT id FROM concepts WHERE entity_type IN ('fund','person'))",
     ["concept_id", "column_id"]),
    # edges between fund/person entities (entity_type lives on entities, not the edge)
    ("entity_relationships",
     "source_id IN (SELECT id FROM entities WHERE entity_type IN ('fund','person')) "
     "AND target_id IN (SELECT id FROM entities WHERE entity_type IN ('fund','person'))",
     ["source_id", "relation_type", "target_id", "valid_from"]),
    # policies + runs (whole table; cluster reconciler owns these going forward)
    ("crawl_policies", "1=1", ["id"]),
    ("policy_runs", "1=1", ["id"]),
]


def copy_table(engine_s, engine_t, table, where, conflict, target_needs_002: bool = False):
    if target_needs_002:
        with engine_t.begin() as c:
            c.execute(text(MIGRATION_002))
        log.info("created crawl_policies/policy_runs on target")
    def _cols(e):
        with e.connect() as c:
            return {r[0] for r in c.execute(text(
                f"SELECT column_name FROM information_schema.columns "
                f"WHERE table_schema='public' AND table_name='{table}'"))}
    cols = sorted(_cols(engine_s) & _cols(engine_t))  # schema drift: copy only shared cols
    col_list = ", ".join(cols)
    conflict_list = ", ".join(conflict)
    upd = ", ".join(f"{c}=EXCLUDED.{c}" for c in cols if c not in conflict)
    with engine_s.connect() as sc:
        rows = sc.execute(text(f"SELECT {col_list} FROM {table} WHERE {where}")).mappings().all()
    if not rows:
        log.info(f"{table}: 0 rows (skip)")
        return

    def _adapt(r):
        # JSONB values come back as dict/list from psycopg2; re-serialize so
        # psycopg2 can adapt them on insert.
        return {k: (json.dumps(v, ensure_ascii=False) if isinstance(v, (dict, list)) else v)
                for k, v in r.items()}

    # psycopg2 execute_values: ONE round trip (executemany over the kubectl
    # port-forward = one round trip per row -> minutes for 5k rows).
    from psycopg2.extras import execute_values
    upd = ", ".join(f"{c}=EXCLUDED.{c}" for c in cols if c not in conflict)
    sql = (f"INSERT INTO {table} ({col_list}) VALUES %s "
           f"ON CONFLICT ({conflict_list}) DO UPDATE SET {upd}")
    with engine_t.begin() as c:
        vals = [tuple(_adapt(dict(r))[k] for k in cols) for r in rows]
        cur = c.connection.driver_connection.cursor()
        try:
            execute_values(cur, sql, vals, page_size=500)
        finally:
            cur.close()
    log.info(f"{table}: copied {len(rows)} rows")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default="postgresql://admin:admin123@192.168.1.4:5433/postgres")
    ap.add_argument("--target",
                    default="postgresql+psycopg2://postgres:admin123@127.0.0.1:55432/postgres")
    args = ap.parse_args()
    eng_s = create_engine(args.source)
    eng_t = create_engine(args.target)
    for table, where, conflict in COPIES:
        copy_table(eng_s, eng_t, table, where, conflict,
                   target_needs_002=(table == "crawl_policies"))
    log.info("done")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Move observations that declare a class their entity is not into quarantine.

Why this exists: production held 351,740 such rows (measured 2026-11-08) —
333,975 stock rows on fund-manager (person) entities (a fund's daily unit NAV,
0.89…1.32, un-mappable to the fund it belongs to), 8,500 on industry entities
(9 HK tickers + 3 ETFs, and no entity of any class carries those codes), 6,302
country indicators on city entities, 2,027 fund rows on stock entities, 936 stock
rows on a fund.

Both ends were sealed first: the write side joins ``entities``, and 0.5.52 makes
reads resolve the entity's own class. What was left was the bookkeeping — the
rows are wrong but their data (a fund's NAV, a HK ticker's price) is not ours to
throw away. Quarantine keeps it, clears the live table, and leaves a `batch_id`
so a later re-homing (once a fund/ETF mapping exists) can move rows back.

The move is one transaction: copy into the quarantine table, then delete exactly
the rows that are there. A failure rolls both back. Re-running is a no-op
(``id`` is the primary key of the quarantine table).

``--restore`` puts a batch back (also one transaction), so this is reversible.

Usage:
    python scripts/quarantine_misattached_observations.py --dry-run
    python scripts/quarantine_misattached_observations.py --db-url <dsn>
    python scripts/quarantine_misattached_observations.py --restore --batch <id>

Exit codes: 0 = done (or nothing to do); 1 = verification mismatch.
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime, timezone

from sqlalchemy import create_engine, text

TABLE = "semantic_observations_quarantine"

CREATE = f"""
CREATE TABLE IF NOT EXISTS {TABLE} (
    id               integer PRIMARY KEY,
    concept_id       integer      NOT NULL,
    entity_type      varchar      NOT NULL,
    entity_id        integer      NOT NULL,
    date             varchar      NOT NULL,
    granularity      varchar      NOT NULL DEFAULT 'day',
    value            varchar,
    unit             varchar,
    source_used      varchar      NOT NULL,
    fetched_at       timestamp    NOT NULL,
    obs_month        varchar,
    declared_type    varchar(32)  NOT NULL,
    entity_actual_type varchar(32) NOT NULL,
    reason           text         NOT NULL,
    batch_id         text         NOT NULL,
    quarantined_at   timestamptz  NOT NULL DEFAULT now()
)
"""

# The predicate the read guard enforces: a row may only belong to an entity of
# the class it declares. --dangling targets the sibling defect instead: rows whose
# entity_id has no entity at all (measured 2026-11-08: 11 price rows on four ids
# that do not exist, written 2026-09-16 by an ingester that resolved ids it never
# created).
MISMATCH = "o.entity_type <> e.entity_type"
DANGLING = "e.id IS NULL"


def _join(predicate: str) -> str:
    """LEFT JOIN when hunting dangling ids, INNER JOIN otherwise."""
    kind = "LEFT JOIN" if predicate is DANGLING else "JOIN"
    return f"FROM semantic_observations o {kind} entities e ON e.id = o.entity_id"


def report(conn, predicate: str = MISMATCH) -> list[tuple[str, str, int]]:
    return [tuple(r) for r in conn.execute(text(f"""
        SELECT o.entity_type, COALESCE(e.entity_type, '<missing>'), count(*)
        {_join(predicate)}
        WHERE {predicate}
        GROUP BY 1, 2 ORDER BY 3 DESC"""))]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db-url", default=os.environ.get("FD_OPEN_DATA_MCP_DATABASE_URL"))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--restore", action="store_true", help="move a batch back")
    ap.add_argument("--dangling", action="store_true",
                    help="target rows whose entity_id has no entity (default: rows whose "
                         "declared class is not their entity's class)")
    ap.add_argument("--batch", help="batch id to restore (default: the newest)")
    ap.add_argument("--reason", default="row declares a class its entity is not")
    args = ap.parse_args()
    if not args.db_url:
        ap.error("no database URL: pass --db-url or set FD_OPEN_DATA_MCP_DATABASE_URL")

    engine = create_engine(args.db_url, pool_pre_ping=True)
    with engine.begin() as conn:
        conn.execute(text("SET statement_timeout = 1800000"))
        conn.execute(text(CREATE))

        if args.restore:
            batch = args.batch or conn.execute(text(
                f"SELECT batch_id FROM {TABLE} ORDER BY quarantined_at DESC LIMIT 1"
            )).scalar()
            if not batch:
                print("nothing quarantined; nothing to restore")
                return 0
            back = conn.execute(text(f"""
                INSERT INTO semantic_observations
                    (id, concept_id, entity_type, entity_id, date, granularity, value,
                     unit, source_used, fetched_at, obs_month)
                SELECT id, concept_id, entity_type, entity_id, date, granularity, value,
                       unit, source_used, fetched_at, obs_month
                FROM {TABLE} WHERE batch_id = :b
                ON CONFLICT (id) DO NOTHING"""), {"b": batch}).rowcount
            gone = conn.execute(text(f"DELETE FROM {TABLE} WHERE batch_id = :b"),
                                {"b": batch}).rowcount
            print(f"restored batch {batch}: re-inserted {back}, removed {gone} from quarantine")
            return 0

        predicate = DANGLING if args.dangling else MISMATCH
        rows = report(conn, predicate)
        total = sum(r[2] for r in rows)
        print(f"mis-attached observations: {total}")
        for declared, actual, n in rows:
            print(f"  {declared:<10} declared on {actual:<10} entities: {n:>7}")
        if not total:
            print("nothing to quarantine")
            return 0
        if args.dry_run:
            print("dry run: nothing written")
            return 0

        batch = "q" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        copied = conn.execute(text(f"""
            INSERT INTO {TABLE}
                (id, concept_id, entity_type, entity_id, date, granularity, value,
                 unit, source_used, fetched_at, obs_month,
                 declared_type, entity_actual_type, reason, batch_id)
            SELECT o.id, o.concept_id, o.entity_type, o.entity_id, o.date, o.granularity,
                   o.value, o.unit, o.source_used, o.fetched_at, o.obs_month,
                   o.entity_type, COALESCE(e.entity_type, '<missing>'), :reason, :batch
            {_join(predicate)}
            WHERE {predicate}
            ON CONFLICT (id) DO NOTHING"""),
            {"reason": args.reason, "batch": batch}).rowcount
        if predicate is DANGLING:
            deleted = conn.execute(text(f"""
                DELETE FROM semantic_observations o
                WHERE NOT EXISTS (SELECT 1 FROM entities e WHERE e.id = o.entity_id)""")).rowcount
        else:
            deleted = conn.execute(text(f"""
                DELETE FROM semantic_observations o
                USING entities e
                WHERE e.id = o.entity_id AND {MISMATCH}""")).rowcount
        held = conn.execute(text(
            f"SELECT count(*) FROM {TABLE} WHERE batch_id = :b"), {"b": batch}).scalar()

    print(f"batch {batch}: copied {copied}, deleted {deleted}, held {held}")
    if copied != deleted or held != deleted:
        print("FAIL: the copy and the delete disagree — inspect before rerunning",
              file=sys.stderr)
        return 1
    print("OK: the targeted rows are quarantined and gone from the live table")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

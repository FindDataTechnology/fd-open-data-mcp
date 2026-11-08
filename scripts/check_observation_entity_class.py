#!/usr/bin/env python3
"""Assert that no observation declares a class its entity does not have.

Why this exists: on 2026-11-08 production held **351,740 observations** whose
declared ``entity_type`` is not the type of the entity they hang off:

======================  ============  ==================================================
row declares            entity is     what the rows actually are
======================  ============  ==================================================
stock                   person        333,975 — a fund's unit NAV (0.89 … 1.32, daily)
                                      written per fund-manager entity; the manager id was
                                      used as the code, so the fund itself is not derivable
stock                   industry      8,500 — 9 HK tickers + 3 ETFs' price/financials, and
                                      no entity of any class carries those codes
country                 city          6,302 — country indicators (gdp, inflation, population)
fund                    stock         2,027 — fund concepts read through stock entities
stock                   fund          936
======================  ============  ==================================================

Total 351,740. (Note these are row-type vs ENTITY-type pairs; grouping the
*concept* type instead also yields region→country 96 and company→stock 12, which
are not row/entity mismatches and must not be counted here — a class that appears
later is then flagged NEW.)

They are now unreachable: the write side joined ``entities`` (0.5.x, so nothing new
is produced) and 0.5.52 resolves the entity's own class on read, so no answer can
surface them. What is left is a bookkeeping question — delete, quarantine, or
leave — which is the owner's call, not a code change.

This check keeps the *known* baseline visible and fails on anything NEW. A new
class of mis-attachment is an incident (it means a writer ignored the entity
join); the historical rows are counted and reported, not failed.

The baseline is per (row type, entity type) with the exact 2026-11-08 counts: a
pair may stay at or below its recorded count, and a pair that was not there at
all fails immediately.

Usage:
    python scripts/check_observation_entity_class.py --db-url <dsn>
    python scripts/check_observation_entity_class.py --db-url <dsn> --show-entities 5

Exit codes: 0 = only the recorded baseline (or nothing); 1 = a new/grown class.
"""
from __future__ import annotations

import argparse
import os
import sys

from sqlalchemy import create_engine, text

# Measured against production on 2026-11-08. Growth is a violation; shrinkage is
# fine (it means someone cleaned part of it).
BASELINE: dict[tuple[str, str], int] = {
    ("stock", "person"): 333_975,
    ("stock", "industry"): 8_500,
    ("country", "city"): 6_302,
    ("fund", "stock"): 2_027,
    ("stock", "fund"): 936,
}


def mismatches(conn) -> list[tuple[str, str, int, int]]:
    return [tuple(r) for r in conn.execute(text("""
        SELECT o.entity_type, e.entity_type, count(*), count(DISTINCT e.id)
        FROM semantic_observations o JOIN entities e ON e.id = o.entity_id
        WHERE o.entity_type <> e.entity_type
        GROUP BY 1, 2 ORDER BY 3 DESC"""))]


def dangling(conn) -> int:
    """Rows whose entity_id has no entity at all (a separate defect, reported)."""
    return conn.execute(text("""
        SELECT count(*) FROM semantic_observations o
        LEFT JOIN entities e ON e.id = o.entity_id WHERE e.id IS NULL""")).scalar() or 0


def sample_entities(conn, row_type: str, entity_type: str, limit: int) -> list[tuple]:
    return [tuple(r) for r in conn.execute(text("""
        SELECT e.id, e.code, count(*) FROM semantic_observations o
        JOIN entities e ON e.id = o.entity_id
        WHERE o.entity_type = :rt AND e.entity_type = :et
        GROUP BY 1, 2 ORDER BY 3 DESC LIMIT :n"""), {"rt": row_type, "et": entity_type, "n": limit})]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db-url", default=os.environ.get("FD_OPEN_DATA_MCP_DATABASE_URL"))
    ap.add_argument("--show-entities", type=int, default=0,
                    help="print the N biggest entities behind each class (default 0)")
    args = ap.parse_args()
    if not args.db_url:
        ap.error("no database URL: pass --db-url or set FD_OPEN_DATA_MCP_DATABASE_URL")

    engine = create_engine(args.db_url, pool_pre_ping=True)
    with engine.connect() as conn:
        conn.execute(text("SET statement_timeout = 600000"))
        rows = mismatches(conn)
        orphans = dangling(conn)
        samples = {
            (r[0], r[1]): sample_entities(conn, r[0], r[1], args.show_entities)
            for r in rows
        } if args.show_entities else {}

    grown: list[tuple[str, str, int, int]] = []
    new: list[tuple[str, str, int, int]] = []
    total = 0
    print("observations whose declared class is not their entity's class:")
    for rt, et, n, ents in rows:
        total += n
        base = BASELINE.get((rt, et))
        if base is None:
            tag = "NEW"
            new.append((rt, et, n, ents))
        elif n > base:
            tag = f"GREW(+{n - base})"
            grown.append((rt, et, n, ents))
        else:
            tag = f"known({base})"
        print(f"  {tag:<16} {rt:<10} on {et:<10} {n:>7} rows / {ents:>5} entities")
        for eid, code, cnt in samples.get((rt, et), []):
            print(f"      entity {eid:<8} {str(code)[:40]:<40} {cnt:>7}")
    print(f"baseline rows total: {total}")
    if orphans:
        print(f"  report   {orphans} rows carry an entity_id that has no entity "
              "(separate defect, not gated)")

    if new or grown:
        print("FAIL: a class of mis-attached observations appeared or grew — a "
              "writer is resolving entities without checking their type",
              file=sys.stderr)
        return 1
    print("OK: only the recorded 2026-11-08 baseline; no new mis-attachment")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

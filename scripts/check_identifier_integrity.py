#!/usr/bin/env python3
"""Assert that every identifier row carries its entity's own type.

Why this exists: on 2026-11-08 the identifier table held 1,793 rows typed
``stock`` whose owning entity was a **person** — akshare's fund-manager frame
had, in an earlier generation of the code, written each manager's managed fund
codes as that manager's stock identifiers. Any code→entity resolution that
trusts the identifier table could therefore return a *person* for an ETF code
(``512660`` resolved to 金泽宇). The rows were removed; this check keeps them
from coming back.

The five pairs below are deliberately deferred: each needs a per-class decision
(a city carrying a country code, an index carrying a stock ticker, …) and they
are small. They are reported, not failed, so a *new* class cannot hide among
them.

Usage:
    python scripts/check_identifier_integrity.py --db-url <dsn>
    python scripts/check_identifier_integrity.py --db-url <dsn> --max-deferred 300

Exit codes: 0 = only the deferred classes (or nothing); 1 = a violation.
"""
from __future__ import annotations

import argparse
import os
import sys

from sqlalchemy import create_engine, text

# (identifier type, entity type) pairs we knowingly leave alone.
DEFERRED = {
    ("stock", "organization"),
    ("stock", "fund"),          # re-typed in the 2026-11-08 repair; kept for old rows
    ("country", "city"),
    ("stock", "industry"),
    ("stock", "index"),
    ("stock", "exchange"),
}


def mismatches(conn) -> list[tuple[str, str, int, int]]:
    return [tuple(r) for r in conn.execute(text("""
        SELECT i.entity_type, e.entity_type, count(*), count(DISTINCT i.entity_id)
        FROM entity_source_identifiers i JOIN entities e ON e.id = i.entity_id
        WHERE i.entity_type <> e.entity_type
        GROUP BY 1, 2 ORDER BY 3 DESC"""))]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db-url", default=os.environ.get("FD_OPEN_DATA_MCP_DATABASE_URL"))
    ap.add_argument("--max-deferred", type=int, default=500,
                    help="tolerate up to N rows across the known deferred pairs (default 500)")
    args = ap.parse_args()
    if not args.db_url:
        ap.error("no database URL: pass --db-url or set FD_OPEN_DATA_MCP_DATABASE_URL")

    engine = create_engine(args.db_url, pool_pre_ping=True)
    with engine.connect() as conn:
        conn.execute(text("SET statement_timeout = 600000"))
        rows = mismatches(conn)

    illegal = [r for r in rows if (r[0], r[1]) not in DEFERRED]
    deferred_total = sum(r[2] for r in rows if (r[0], r[1]) in DEFERRED)

    print(f"mismatched (identifier_type, entity_type) pairs: {len(rows)}")
    for it, et, n, ents in rows:
        tag = "deferred" if (it, et) in DEFERRED else "VIOLATION"
        print(f"  {tag:<9} {it:<14} on {et:<14} {n:>6} rows / {ents:>5} entities")
    print(f"deferred rows total: {deferred_total} (limit {args.max_deferred})")

    if illegal:
        print("FAIL: identifier rows whose type disagrees with their entity, "
              "outside the deferred classes", file=sys.stderr)
        return 1
    if deferred_total > args.max_deferred:
        print(f"FAIL: deferred mismatches grew past {args.max_deferred}", file=sys.stderr)
        return 1
    print("OK: no identifier row claims a type its entity does not have")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

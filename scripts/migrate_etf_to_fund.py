#!/usr/bin/env python3
"""Migrate entity_type 'etf' -> 'fund' in place (add-fund-crawl-control-center, D1).

Exchange-traded funds are canonical `fund` entities whose subtype lives in
`metadata_json.subtype = 'etf'`. This script rewrites the 6 drifted `etf`
entity rows in place (id-preserving) and stamps `subtype` into metadata_json.

Idempotent: re-running is a no-op once zero `etf` rows remain.

Usage:
    python scripts/migrate_etf_to_fund.py            # forward: etf -> fund
    python scripts/migrate_etf_to_fund.py --reverse  # reverse: fund(subtype=etf) -> etf
    python scripts/migrate_etf_to_fund.py --dry-run  # print, do not commit

DB URL: FD_OPEN_DATA_MCP_DATABASE_URL env, else the package default (live PG).
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from sqlalchemy import create_engine, text

DEFAULT_URL = "postgresql://admin:admin123@192.168.1.4:5433/postgres"


def _url() -> str:
    return os.environ.get("FD_OPEN_DATA_MCP_DATABASE_URL", DEFAULT_URL)


def forward(conn, dry_run: bool) -> int:
    rows = conn.execute(
        text("SELECT id, code, metadata_json FROM entities WHERE entity_type='etf' ORDER BY id")
    ).fetchall()
    for rid, code, meta in rows:
        meta = dict(meta or {})
        meta.setdefault("subtype", "etf")
        print(f"  entity {rid} ({code}): etf -> fund, metadata_json.subtype='etf'")
        if not dry_run:
            conn.execute(
                text(
                    "UPDATE entities SET entity_type='fund',"
                    " metadata_json=CAST(:meta AS jsonb),"
                    " updated_at=now() WHERE id=:id AND entity_type='etf'"
                ),
                {"meta": json.dumps(meta), "id": rid},
            )
    # Nothing else references entity_type='etf' on the live DB (identifiers,
    # relationships, observations, concepts all verified empty), but rewrite
    # defensively where entity_type is stored per-row.
    for table, col in (("semantic_observations", "entity_type"),
                       ("entity_source_identifiers", "entity_type"),
                       ("concepts", "entity_type")):
        n = conn.execute(text(f"SELECT count(*) FROM {table} WHERE {col}='etf'")).scalar()
        if n:
            print(f"  {table}: {n} rows etf -> fund")
            if not dry_run:
                conn.execute(text(f"UPDATE {table} SET {col}='fund' WHERE {col}='etf'"))
    return len(rows)


def reverse(conn, dry_run: bool) -> int:
    rows = conn.execute(
        text(
            "SELECT id, code FROM entities WHERE entity_type='fund'"
            " AND metadata_json->>'subtype' = 'etf' ORDER BY id"
        )
    ).fetchall()
    for rid, code in rows:
        print(f"  entity {rid} ({code}): fund -> etf (subtype removed)")
        if not dry_run:
            conn.execute(
                text(
                    "UPDATE entities SET entity_type='etf',"
                    " metadata_json = metadata_json - 'subtype',"
                    " updated_at=now() WHERE id=:id"
                ),
                {"id": rid},
            )
    return len(rows)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--reverse", action="store_true", help="undo the migration")
    ap.add_argument("--dry-run", action="store_true", help="print actions, do not commit")
    args = ap.parse_args()

    eng = create_engine(_url())
    with eng.connect() as conn:
        n = reverse(conn, args.dry_run) if args.reverse else forward(conn, args.dry_run)
        if args.dry_run:
            conn.rollback()
            print(f"dry-run: would migrate {n} entities (rolled back)")
        else:
            conn.commit()
            print(f"done: {n} entities migrated")
    return 0


if __name__ == "__main__":
    sys.exit(main())

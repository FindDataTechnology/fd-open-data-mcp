"""Seed money-market funds (货币基金) into the fund entity universe.

`seed_fund_universe.py` seeds the top-N funds by AUM; money funds have no bulk
AUM source upstream so they rank last and get cut. This script backfills them:

- `fund_money_fund_daily_em()` returns ALL money funds in one call (code +
  基金简称 + 成立日期 + 基金经理), so seeding is a single full-market round trip.
- Upserts `entities` (entity_type='fund', subtype='money') + `entity_source_identifiers`
  (source='akshare', identifier=基金代码), idempotent by (entity_type, code).

Run this BEFORE `snapshot_ingest_funds.py --skip-rank --skip-rating` so the
money-fund yield concepts (yield.7day_annualized / yield.per_10k) have entities
to bind to.

Usage (scraw-fd-open-data-mcp venv has akshare):
    /Users/chengsishi/finddata/scraw-fd-open-data-mcp/.venv/bin/python \
        scripts/seed_money_funds.py
"""
from __future__ import annotations

import argparse
import datetime
import json
import logging
import sys

from sqlalchemy import create_engine, text

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("seed-money-funds")

ENTITY_TYPE = "fund"
SOURCE = "akshare"


def _fmt(v) -> str | None:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def fetch_money_funds() -> list[dict]:
    import akshare as ak

    df = ak.fund_money_fund_daily_em()
    if df is None or df.empty or "基金代码" not in df.columns:
        return []
    out = []
    for r in df.to_dict("records"):
        code = _fmt(r.get("基金代码"))
        if not code:
            continue
        out.append({
            "code": code,
            "name": _fmt(r.get("基金简称")),
            "inception_date": _fmt(r.get("成立日期")),
            "managers": _fmt(r.get("基金经理")),
        })
    return out


def upsert(engine, funds: list[dict]) -> int:
    if not funds:
        return 0
    now = datetime.datetime.utcnow()
    rows = []
    for f in funds:
        meta = {
            "subtype": "money",
            "fund_type": "货币型",
            "inception_date": f["inception_date"],
            "managers": f["managers"],
            "seed": "seed_money_funds",
        }
        rows.append((ENTITY_TYPE, f["code"], f["name"],
                     json.dumps(meta, ensure_ascii=False), now))

    # Upsert entities (no RETURNING: execute_values page_size splits the batch
    # across multiple statements, and cur.fetchall() would only see the last page).
    entity_sql = (
        "INSERT INTO entities (entity_type, code, name_zh, metadata_json, updated_at) "
        "VALUES %s "
        "ON CONFLICT (entity_type, code) DO UPDATE SET "
        "name_zh=EXCLUDED.name_zh, metadata_json=EXCLUDED.metadata_json, updated_at=EXCLUDED.updated_at"
    )
    with engine.begin() as conn:
        cur = conn.connection.driver_connection.cursor()
        try:
            from psycopg2.extras import execute_values
            execute_values(cur, entity_sql, rows, page_size=500)
        finally:
            cur.close()

    # Re-resolve the entity ids for the identifier link.
    codes = [f["code"] for f in funds]
    with engine.connect() as conn:
        got = conn.execute(text(
            "SELECT id, code FROM entities WHERE entity_type=:et AND code = ANY(:codes)"
        ), {"et": ENTITY_TYPE, "codes": codes}).all()
    id_by_code = {str(r[1]).strip(): r[0] for r in got}

    ident_rows = [(ENTITY_TYPE, eid, SOURCE, code, now)
                  for code, eid in id_by_code.items()]
    ident_sql = (
        "INSERT INTO entity_source_identifiers (entity_type, entity_id, source, identifier, created_at) "
        "VALUES %s "
        "ON CONFLICT (entity_type, entity_id, source) DO UPDATE SET identifier=EXCLUDED.identifier"
    )
    with engine.begin() as conn:
        cur = conn.connection.driver_connection.cursor()
        try:
            from psycopg2.extras import execute_values
            execute_values(cur, ident_sql, ident_rows, page_size=500)
        finally:
            cur.close()
    return len(id_by_code)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db-url",
                    default="postgresql+psycopg2://postgres:admin123@127.0.0.1:55432/postgres")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    log.info("fetching fund_money_fund_daily_em ...")
    funds = fetch_money_funds()
    log.info("universe: %d money funds", len(funds))
    if not funds:
        return 1
    if args.dry_run:
        for f in funds[:10]:
            log.info("  %s %s", f["code"], f["name"])
        log.info("(dry-run: %d total, not writing)", len(funds))
        return 0

    engine = create_engine(args.db_url)
    n = upsert(engine, funds)
    log.info("upserted %d money fund entities + identifiers", n)
    return 0


if __name__ == "__main__":
    sys.exit(main())

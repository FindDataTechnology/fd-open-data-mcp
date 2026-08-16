"""Bulk-ingest full-history ETF OHLCV into semantic_observations.

The fund universe has 119 akshare-identified ETFs (510050, 510300, 159915, ...)
but only 7 have any price data, and only `price.close` was ever written. This
script backfills the full price frame for every ETF in one pass.

Source: `fund_etf_hist_sina(symbol)` (finance.sina.com.cn, one call per ETF)
  -> date/open/high/low/close/volume/amount
    -> price.open (383) / price.close (384) / price.high (385) / price.low (386)
    -> price.volume (387, unit=shares) / price.amount (388, unit=currency_cny)

We deliberately use sina rather than eastmoney's `fund_etf_hist_em`: the
push2his.eastmoney.com kline endpoint drops the connection from this Mac (and
its volume unit is 手, which mismatches the `shares` concept unit). sina's
`volume` is already in shares, matching the ontology.

Full history is returned per ETF (start_date filter is client-side only);
re-running is idempotent (ON CONFLICT by concept/entity/date/granularity).

Usage (scraw-fd-open-data-mcp venv has akshare):
    /Users/chengsishi/finddata/scraw-fd-open-data-mcp/.venv/bin/python \
        scripts/bulk_ingest_etf_hist.py [--limit N] [--start YYYYMMDD]
"""
from __future__ import annotations

import argparse
import datetime
import logging
import sys
import time

from sqlalchemy import create_engine, text
from psycopg2.extras import execute_values

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("etf-hist-ingest")

ENTITY_TYPE = "fund"
SOURCE = "akshare"

# concept code -> sina column
ETF_CONCEPTS = {
    "price.open": "open",
    "price.close": "close",
    "price.high": "high",
    "price.low": "low",
    "price.volume": "volume",
    "price.amount": "amount",
}


def _fmt(v) -> str | None:
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f != f:  # NaN
        return None
    s = repr(f)
    if s.endswith(".0"):
        s = s[:-2]
    return s


def _sina_symbol(code: str) -> str:
    """Bare ETF code -> sina symbol with exchange prefix (5xxxxx=SH, 1xxxxx=SZ)."""
    code = code.strip()
    return ("sh" if code.startswith("5") else "sz") + code


def load_etf_identifiers(engine) -> list[tuple[str, int]]:
    """(akshare_code, entity_id) for all ETF entities with an akshare identifier."""
    with engine.connect() as c:
        rows = c.execute(text(
            "SELECT esi.identifier, esi.entity_id "
            "FROM entity_source_identifiers esi "
            "JOIN entities e ON e.id = esi.entity_id "
            "WHERE esi.entity_type=:et AND esi.source=:src "
            "AND (e.metadata_json::text LIKE '%\"subtype\": \"etf\"%' "
            "     OR e.metadata_json::text LIKE '%\"subtype\":\"etf\"%') "
            "ORDER BY esi.entity_id"
        ), {"et": ENTITY_TYPE, "src": SOURCE}).all()
    return [(str(r[0]).strip(), r[1]) for r in rows]


def load_concepts(engine, codes: list[str]) -> dict[str, dict]:
    if not codes:
        return {}
    with engine.connect() as c:
        rows = c.execute(text(
            "SELECT id, code, unit FROM concepts WHERE entity_type=:et AND code = ANY(:codes)"
        ), {"et": ENTITY_TYPE, "codes": codes}).all()
    return {r[1]: {"id": r[0], "unit": r[2]} for r in rows}


def upsert(engine, rows: list[dict]) -> int:
    if not rows:
        return 0
    cols = ["concept_id", "entity_type", "entity_id", "date", "value",
            "unit", "source_used", "fetched_at", "granularity"]
    upd = "value=EXCLUDED.value, unit=EXCLUDED.unit, source_used=EXCLUDED.source_used, fetched_at=EXCLUDED.fetched_at"
    sql = (f"INSERT INTO semantic_observations ({', '.join(cols)}) VALUES %s "
           f"ON CONFLICT (concept_id, entity_type, entity_id, date, granularity) "
           f"DO UPDATE SET {upd}")
    vals = [tuple(r[c] for c in cols) for r in rows]
    last = None
    for attempt in range(5):
        try:
            with engine.begin() as conn:
                cur = conn.connection.driver_connection.cursor()
                try:
                    execute_values(cur, sql, vals, page_size=500)
                finally:
                    cur.close()
            return len(vals)
        except Exception as e:  # noqa: BLE001
            last = e
            engine.dispose()  # drop severed pooled connections
            time.sleep(2 * (attempt + 1))
    raise last


def already_done(engine, concepts) -> set[int]:
    """entity_ids that already have price.close day rows (resume skip)."""
    meta = concepts.get("price.close")
    if meta is None:
        return set()
    with engine.connect() as c:
        rows = c.execute(text(
            "SELECT DISTINCT entity_id FROM semantic_observations "
            "WHERE concept_id=:cid AND entity_type=:et AND granularity='day'"
        ), {"cid": meta["id"], "et": ENTITY_TYPE}).all()
    return {r[0] for r in rows}


def ingest(engine, ids, concepts, start_date, limit, skip_done):
    import akshare as ak

    now = datetime.datetime.utcnow()
    total = done = failed = skipped = 0
    for i, (ident, eid) in enumerate(ids, 1):
        if eid in skip_done:
            skipped += 1
            log.info("[%3d/%3d] %s (already done, skip)", i, len(ids), ident)
            continue
        sym = _sina_symbol(ident)
        try:
            df = ak.fund_etf_hist_sina(symbol=sym)
        except Exception as e:  # noqa: BLE001
            failed += 1
            log.info("[%3d/%3d] %s FAILED: %s", i, len(ids), ident, e)
            continue
        if df is None or df.empty or "date" not in df.columns:
            failed += 1
            continue
        # Build this ETF's rows only, then upsert (memory-bounded + resumable).
        rows = []
        for r in df.itertuples(index=False):
            date = str(getattr(r, "date", "")).strip()[:10]
            if len(date) < 10 or date[4] != "-":
                continue
            if start_date and date < start_date:
                continue
            for code, col in ETF_CONCEPTS.items():
                meta = concepts.get(code)
                if meta is None:
                    continue
                val = _fmt(getattr(r, col, None))
                if val is None:
                    continue
                rows.append({
                    "concept_id": meta["id"], "entity_type": ENTITY_TYPE, "entity_id": eid,
                    "date": date, "value": val, "unit": meta["unit"],
                    "source_used": SOURCE, "fetched_at": now, "granularity": "day",
                })
        n = upsert(engine, rows)
        total += n
        done += 1
        log.info("[%3d/%3d] %s %s: %d obs", i, len(ids), ident, sym, n)
        time.sleep(0.15)
    log.info("etf hist: %d obs written (%d fetched, %d failed, %d skipped)",
             total, done, failed, skipped)
    return total


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db-url",
                    default="postgresql+psycopg2://postgres:admin123@127.0.0.1:55432/postgres")
    ap.add_argument("--start", default=None, help="start date YYYY-MM-DD (optional filter)")
    ap.add_argument("--limit", type=int, default=None, help="cap ETF entities (testing)")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    engine = create_engine(args.db_url)
    ids = load_etf_identifiers(engine)
    log.info("ETF identifier map: %d entities", len(ids))
    if not ids:
        log.error("no akshare ETF identifiers found")
        return 1
    if args.limit:
        ids = ids[:args.limit]

    concepts = load_concepts(engine, list(ETF_CONCEPTS))
    log.info("concepts resolved: %s", ", ".join(concepts))

    if args.dry_run:
        for ident, eid in ids:
            log.info("  %s -> %s (entity %d)", ident, _sina_symbol(ident), eid)
        log.info("(dry-run: %d ETFs, not writing)", len(ids))
        return 0

    skip_done = already_done(engine, concepts)
    log.info("resume skip: %d ETFs already have price.close rows", len(skip_done))

    n = ingest(engine, ids, concepts, args.start, args.limit, skip_done)
    log.info("done: %d observations upserted", n)
    return 0


if __name__ == "__main__":
    sys.exit(main())

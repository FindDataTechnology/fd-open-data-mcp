"""Bulk-ingest full-history A-share stock OHLCV into semantic_observations.

Same pattern as bulk_ingest_etf_hist.py: the crawl pipeline only ever wrote
`price.close` (and only ~1 month of it), so this backfills the full price frame
(open/close/high/low/volume/amount) for every A-share stock with an akshare
identifier, in one pass.

Source: `stock_zh_a_daily(symbol, adjust='')` (sina, one call per stock)
  -> date/open/high/low/close/volume/amount/outstanding_share/turnover
    -> price.open (233) / price.close (234) / price.high (235) / price.low (236)
    -> price.volume (237, unit=shares) / price.amount (238, unit=currency_cny)

`sina` `volume` is already in shares, matching the ontology. Full history is
returned per stock (start_date filter is client-side only); re-running is
idempotent (ON CONFLICT by concept/entity/date/granularity) and resumable.

Sharding: pass --shard K --num-shards N to split the stock universe across N
parallel workers (each writes a disjoint slice; resume-skip dedups on overlap).

Only A-shares are fetched: code prefix 6 -> sh, 0/3 -> sz. Codes with other
prefixes (1/5 = ETFs/funds mis-seeded as stock, 9 = USD B-shares) are skipped
to keep the CNY `currency` unit consistent.

Usage (scraw-fd-open-data-mcp venv has akshare):
    /Users/chengsishi/finddata/scraw-fd-open-data-mcp/.venv/bin/python \
        scripts/bulk_ingest_stock_hist.py --shard 0 --num-shards 8
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
log = logging.getLogger("stock-hist-ingest")

ENTITY_TYPE = "stock"
SOURCE = "akshare"

# concept code -> sina column
STOCK_CONCEPTS = {
    "price.open": "open",
    "price.close": "close",
    "price.high": "high",
    "price.low": "low",
    "price.volume": "volume",
    "price.amount": "amount",
}

# CNY-unit concept set (the USD `currency_usd` variants are excluded).
CNY_UNITS = ("currency", "shares")


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


def _sina_symbol(code: str) -> str | None:
    """Bare A-share code -> sina symbol (6xxxxx=SH, 0/3xxxxx=SZ). None if not A-share."""
    code = code.strip()
    if code.startswith("6"):
        return "sh" + code
    if code.startswith(("0", "3")):
        return "sz" + code
    return None  # 1/5 = funds/ETFs, 9 = USD B-share -> skip (unit mismatch)


def load_stock_identifiers(engine) -> list[tuple[str, int]]:
    """(akshare_code, entity_id) for all A-share stocks with an akshare identifier."""
    with engine.connect() as c:
        rows = c.execute(text(
            "SELECT esi.identifier, esi.entity_id "
            "FROM entity_source_identifiers esi "
            "WHERE esi.entity_type=:et AND esi.source=:src "
            "ORDER BY esi.entity_id"
        ), {"et": ENTITY_TYPE, "src": SOURCE}).all()
    out = []
    for r in rows:
        code = str(r[0]).strip()
        if _sina_symbol(code) is not None:
            out.append((code, r[1]))
    return out


def load_concepts(engine, codes: list[str]) -> dict[str, dict]:
    if not codes:
        return {}
    with engine.connect() as c:
        rows = c.execute(text(
            "SELECT id, code, unit FROM concepts "
            "WHERE entity_type=:et AND code = ANY(:codes) AND unit = ANY(:units)"
        ), {"et": ENTITY_TYPE, "codes": codes, "units": list(CNY_UNITS)}).all()
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
    """entity_ids that already have full OHLCV (price.open day rows = backfilled).

    We key resume-skip on `price.open`, NOT `price.close`: the crawl pipeline only
    ever wrote close, so close exists for ~6070 stocks but open/high/low/volume/
    amount are entirely absent. Skipping on close would skip the whole backfill.
    """
    meta = concepts.get("price.open")
    if meta is None:
        return set()
    with engine.connect() as c:
        rows = c.execute(text(
            "SELECT DISTINCT entity_id FROM semantic_observations "
            "WHERE concept_id=:cid AND entity_type=:et AND granularity='day'"
        ), {"cid": meta["id"], "et": ENTITY_TYPE}).all()
    return {r[0] for r in rows}


def ingest(engine, ids, concepts, start_date, skip_done):
    import akshare as ak

    now = datetime.datetime.now(datetime.timezone.utc)
    total = done = failed = skipped = 0
    for i, (ident, eid) in enumerate(ids, 1):
        if eid in skip_done:
            skipped += 1
            log.info("[%3d/%3d] %s (already done, skip)", i, len(ids), ident)
            continue
        sym = _sina_symbol(ident)
        if sym is None:
            skipped += 1
            continue
        try:
            df = ak.stock_zh_a_daily(symbol=sym, adjust="")
        except Exception as e:  # noqa: BLE001
            failed += 1
            log.info("[%3d/%3d] %s FAILED: %s", i, len(ids), ident, e)
            continue
        if df is None or df.empty or "date" not in df.columns:
            failed += 1
            continue
        rows = []
        for r in df.itertuples(index=False):
            date = str(getattr(r, "date", "")).strip()[:10]
            if len(date) < 10 or date[4] != "-":
                continue
            if start_date and date < start_date:
                continue
            for code, col in STOCK_CONCEPTS.items():
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
        time.sleep(0.1)
    log.info("stock hist: %d obs written (%d fetched, %d failed, %d skipped)",
             total, done, failed, skipped)
    return total


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db-url",
                    default="postgresql+psycopg2://postgres:admin123@127.0.0.1:55432/postgres")
    ap.add_argument("--start", default=None, help="start date YYYY-MM-DD (optional filter)")
    ap.add_argument("--limit", type=int, default=None, help="cap stocks per shard (testing)")
    ap.add_argument("--shard", type=int, default=0, help="0-indexed shard id")
    ap.add_argument("--num-shards", type=int, default=1, help="total shard count")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    engine = create_engine(args.db_url)
    ids = load_stock_identifiers(engine)
    log.info("A-share identifier map: %d stocks", len(ids))
    if not ids:
        log.error("no akshare A-share identifiers found")
        return 1

    # shard the ordered list (disjoint slices)
    if args.num_shards > 1:
        ids = ids[args.shard::args.num_shards]
        log.info("shard %d/%d: %d stocks", args.shard, args.num_shards, len(ids))
    if args.limit:
        ids = ids[:args.limit]

    concepts = load_concepts(engine, list(STOCK_CONCEPTS))
    log.info("concepts resolved: %s", ", ".join(concepts))

    if args.dry_run:
        for ident, eid in ids[:10]:
            log.info("  %s -> %s (entity %d)", ident, _sina_symbol(ident), eid)
        log.info("(dry-run: %d stocks, not writing)", len(ids))
        return 0

    skip_done = already_done(engine, concepts)
    log.info("resume skip: %d stocks already have full OHLCV (price.open rows)", len(skip_done))

    n = ingest(engine, ids, concepts, args.start, skip_done)
    log.info("done: %d observations upserted", n)
    return 0


if __name__ == "__main__":
    sys.exit(main())

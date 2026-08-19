"""Bulk-ingest A-share quarterly financials into semantic_observations.

Backfills balance sheet (BS_*), profit statement (PS_*), and cash flow (CF_*)
for all A-share stocks with akshare identifiers, using eastmoney per-stock calls.

Source:
  - BS: stock_balance_sheet_by_report_em(symbol="SH600519")  -> ~319 cols
  - PS: stock_profit_sheet_by_report_em(symbol="SH600519")   -> ~203 cols
  - CF: stock_cash_flow_sheet_by_report_em(symbol="SH600519") -> ~254 cols

Why eastmoney only (no sina): the original script also called sina's
stock_financial_analysis_indicator for 24 FIN_* ratio concepts, but sina
rate-limits the cluster IP — the call hangs indefinitely (no HTTP timeout in
akshare), causing every stock to hit the SIGALRM and stalling the job.
The EM variant (stock_financial_analysis_indicator_em) is broken in akshare
1.18.83 (returns None -> TypeError). The FIN_* ratios (ROE, EPS, debt ratio,
etc.) are derivable from the raw BS/PS/CF statements, so dropping them loses
no fundamental data.

Concurrency note: launching 16 pods × 3 calls = 48 simultaneous eastmoney
connections triggers IP-level rate limiting — every pod's first stock hangs
until the SIGALRM fires. The fix is FEWER pods (8, not 16) so peak concurrency
stays under eastmoney's threshold, PLUS per-call SIGALRM so a single hung call
is skipped (not the whole stock) and resume-skip so re-runs don't re-fetch.

Timeouts: two layers.
  1. socket.setdefaulttimeout(60) — per-recv socket timeout (backup).
  2. Per-call SIGALRM 120s — each akshare call gets its own alarm. If a call
     hangs (rate-limit), StockTimeout fires, that ONE call is skipped, and
     the remaining statements for the stock still run. This is the critical
     difference from the old per-stock 240s alarm which abandoned an entire
     stock when any single call hung.

Date convention: quarterly -> granularity 'day' with quarter-end dates
(2026-03-31, 2026-06-30, 2026-09-30, 2026-12-31).

Entity type: BS_*/PS_*/CF_* concepts reassigned from 'symbol' to 'stock'
(0 prior obs; safe) so quarterly financials join with OHLCV under one
entity_type and are readable via dispatch (check_applicability passes).

Resume-skip: stocks that already have BS_TOTAL_ASSETS observations are
skipped (idempotent upsert anyway, but skipping saves eastmoney API calls
and avoids re-triggering rate limits on already-crawled stocks).

Sharding: pass --shard K --num-shards N to split the stock universe.

Usage (scraw-fd-open-data-mcp venv has akshare):
    /Users/chengsishi/finddata/scraw-fd-open-data-mcp/.venv/bin/python \\
        scripts/bulk_ingest_stock_financials.py --shard 0 --num-shards 8
"""
from __future__ import annotations

import argparse
import datetime
import logging
import signal
import socket
import sys
import time

from sqlalchemy import create_engine, text
from psycopg2.extras import execute_values

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("stock-financials-ingest")

ENTITY_TYPE = "stock"
SOURCE = "akshare"
CALL_TIMEOUT = 120  # per-call SIGALRM (each BS/PS/CF call)
SOCKET_TIMEOUT = 60  # per-recv socket timeout (backup)

# --------------------------------------------------------------------------
# Concept code -> eastmoney English column name (verified from probe output)
# --------------------------------------------------------------------------
BS_EM_COLS = {
    "BS_MONETARY_CAPITAL": "MONETARYFUNDS",
    "BS_ACCOUNTS_RECEIVABLE": "ACCOUNTS_RECE",
    "BS_INVENTORY": "INVENTORY",
    "BS_CURRENT_ASSETS": "TOTAL_CURRENT_ASSETS",
    "BS_FIXED_ASSETS": "FIXED_ASSET",
    "BS_CONSTRUCTION_IN_PROGRESS": "CIP",
    "BS_INTANGIBLE_ASSETS": "INTANGIBLE_ASSET",
    "BS_GOODWILL": "GOODWILL",
    "BS_LONG_TERM_EQUITY": "LONG_EQUITY_INVEST",
    "BS_NON_CURRENT_ASSETS": "TOTAL_NONCURRENT_ASSETS",
    "BS_TOTAL_ASSETS": "TOTAL_ASSETS",
    "BS_ACCOUNTS_PAYABLE": "ACCOUNTS_PAYABLE",
    "BS_CURRENT_LIABILITIES": "TOTAL_CURRENT_LIAB",
    "BS_NON_CURRENT_LIABILITIES": "TOTAL_NONCURRENT_LIAB",
    "BS_TOTAL_LIABILITIES": "TOTAL_LIABILITIES",
    "BS_EQUITY_PARENT": "TOTAL_PARENT_EQUITY",
    "BS_MINORITY_EQUITY": "MINORITY_EQUITY",
    "BS_TOTAL_EQUITY": "TOTAL_EQUITY",
}

PS_EM_COLS = {
    "PS_REVENUE": "OPERATE_INCOME",
    "PS_COST_OF_SALES": "OPERATE_COST",
    "PS_SELLING_EXPENSE": "SALE_EXPENSE",
    "PS_ADMIN_EXPENSE": "MANAGE_EXPENSE",
    "PS_RD_EXPENSE": "RESEARCH_EXPENSE",
    "PS_FINANCE_EXPENSE": "FINANCE_EXPENSE",
    "PS_INVESTMENT_INCOME": "INVEST_INCOME",
    "PS_OPERATING_PROFIT": "OPERATE_PROFIT",
    "PS_TOTAL_PROFIT": "TOTAL_PROFIT",
    "PS_INCOME_TAX": "INCOME_TAX",
    "PS_NET_PROFIT": "NETPROFIT",
    "PS_NET_PROFIT_PARENT": "PARENT_NETPROFIT",
    "PS_MINORITY_INTEREST": "MINORITY_INTEREST",
    "PS_OTHER_INCOME": "OTHER_INCOME",
    "PS_BASIC_EPS": "BASIC_EPS",
    "PS_DILUTED_EPS": "DILUTED_EPS",
}

CF_EM_COLS = {
    "CF_CASH_RECEIVED_SALES": "SALES_SERVICES",
    "CF_CASH_PAID_GOODS": "BUY_SERVICES",
    "CF_CASH_PAID_STAFF": "PAY_STAFF_CASH",
    "CF_CASH_PAID_TAXES": "PAY_ALL_TAX",
    "CF_OPERATING": "NETCASH_OPERATE",
    "CF_INVESTING": "NETCASH_INVEST",
    "CF_FINANCING": "NETCASH_FINANCE",
    "CF_NET": "CCE_ADD",
}


# --------------------------------------------------------------------------
# SIGALRM timeout infrastructure
# --------------------------------------------------------------------------
class StockTimeout(BaseException):
    """Raised when a single akshare call exceeds CALL_TIMEOUT. BaseException
    so it is NOT swallowed by the per-call ``except Exception`` — it is caught
    explicitly by the ``except StockTimeout`` handler in ``_call_em``."""


def _alarm_handler(signum, frame):
    raise StockTimeout()


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
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


def _em_symbol(code: str) -> str | None:
    """Bare A-share code -> eastmoney symbol (6xxxxx=SH, 0/3xxxxx=SZ)."""
    code = code.strip()
    if code.startswith("6"):
        return "SH" + code
    if code.startswith(("0", "3")):
        return "SZ" + code
    return None  # 1/5 = funds/ETFs, 9 = USD B-share -> skip


def _parse_date(d) -> str | None:
    """Parse any date cell (datetime.date, pd.Timestamp, str) -> 'YYYY-MM-DD'."""
    if d is None:
        return None
    if hasattr(d, "isoformat"):
        return d.isoformat()[:10]
    s = str(d).strip()[:10]
    if len(s) == 10 and s[4] == "-":
        return s
    return None


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
        if _em_symbol(code) is not None:
            out.append((code, r[1]))
    return out


def load_done_entity_ids(engine) -> set[int]:
    """entity_ids that already have BS_TOTAL_ASSETS obs (resume-skip)."""
    with engine.connect() as c:
        rows = c.execute(text(
            "SELECT DISTINCT entity_id FROM semantic_observations so "
            "JOIN concepts c ON c.id = so.concept_id "
            "WHERE so.entity_type=:et AND c.entity_type=:et AND c.code='BS_TOTAL_ASSETS'"
        ), {"et": ENTITY_TYPE}).all()
    return {r[0] for r in rows}


def load_concepts(engine, codes: list[str]) -> dict[str, dict]:
    if not codes:
        return {}
    with engine.connect() as c:
        rows = c.execute(text(
            "SELECT id, code, unit FROM concepts "
            "WHERE entity_type=:et AND code = ANY(:codes)"
        ), {"et": ENTITY_TYPE, "codes": codes}).all()
    return {r[1]: {"id": r[0], "unit": r[2]} for r in rows}


def upsert(engine, rows: list[dict]) -> int:
    if not rows:
        return 0
    cols = ["concept_id", "entity_type", "entity_id", "date", "value",
            "unit", "source_used", "fetched_at", "granularity"]
    upd = ("value=EXCLUDED.value, unit=EXCLUDED.unit, "
           "source_used=EXCLUDED.source_used, fetched_at=EXCLUDED.fetched_at")
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
            engine.dispose()
            time.sleep(2 * (attempt + 1))
    raise last


def _extract(df, date_col, col_map, concepts, eid, now) -> list[dict]:
    """Extract observation rows from a DataFrame."""
    if df is None or df.empty or date_col not in df.columns:
        return []
    rows = []
    for _, r in df.iterrows():
        date = _parse_date(r.get(date_col))
        if not date:
            continue
        for code, col in col_map.items():
            meta = concepts.get(code)
            if meta is None:
                continue
            val = _fmt(r.get(col))
            if val is None:
                continue
            rows.append({
                "concept_id": meta["id"], "entity_type": ENTITY_TYPE,
                "entity_id": eid, "date": date, "value": val,
                "unit": meta["unit"], "source_used": SOURCE,
                "fetched_at": now, "granularity": "day",
            })
    return rows


def _call_em(ak, label, ident, em_sym, fn, col_map, concepts, eid, now):
    """Call one eastmoney function with its own SIGALRM. Returns obs rows.

    If the call hangs past CALL_TIMEOUT, StockTimeout fires — caught here so
    the caller (ingest_one_stock) can proceed to the next statement. If the
    call raises any other error, it's logged and we return [] for this call.
    """
    try:
        signal.alarm(CALL_TIMEOUT)
        df = fn(symbol=em_sym)
        signal.alarm(0)
        return _extract(df, "REPORT_DATE", col_map, concepts, eid, now)
    except StockTimeout:
        signal.alarm(0)
        log.warning("%s %s CALL_TIMEOUT (%ds)", label, ident, CALL_TIMEOUT)
        return []
    except Exception as e:  # noqa: BLE001
        signal.alarm(0)
        log.warning("%s %s FAILED: %s", label, ident, e)
        return []


def ingest_one_stock(ak, ident, eid, bs_c, ps_c, cf_c, now):
    """Fetch BS/PS/CF for one stock. Each call is independently alarm-guarded
    so a single hung call doesn't abandon the whole stock."""
    em_sym = _em_symbol(ident)
    if em_sym is None:
        return []

    rows = []
    rows.extend(_call_em(ak, "BS", ident, em_sym,
                         ak.stock_balance_sheet_by_report_em, BS_EM_COLS, bs_c, eid, now))
    rows.extend(_call_em(ak, "PS", ident, em_sym,
                         ak.stock_profit_sheet_by_report_em, PS_EM_COLS, ps_c, eid, now))
    rows.extend(_call_em(ak, "CF", ident, em_sym,
                         ak.stock_cash_flow_sheet_by_report_em, CF_EM_COLS, cf_c, eid, now))
    return rows


def ingest(engine, ids, done_ids, bs_c, ps_c, cf_c, shard):
    import akshare as ak

    # Stagger: desynchronize pods so they don't all hit eastmoney at the same
    # instant. Pod K sleeps K*5s before its first call.
    if shard:
        time.sleep(shard * 5)

    # Safety net: if eastmoney stops sending data, recv() raises after 60s.
    socket.setdefaulttimeout(SOCKET_TIMEOUT)

    signal.signal(signal.SIGALRM, _alarm_handler)

    now = datetime.datetime.now(datetime.timezone.utc)
    total = done = failed = skipped = 0
    for i, (ident, eid) in enumerate(ids, 1):
        if eid in done_ids:
            skipped += 1
            continue
        try:
            rows = ingest_one_stock(ak, ident, eid, bs_c, ps_c, cf_c, now)
            n = upsert(engine, rows)
            total += n
            done += 1
            if i % 10 == 0 or n == 0:
                log.info("[%3d/%3d] %s: %d obs", i, len(ids), ident, n)
        except Exception as e:  # noqa: BLE001
            log.warning("[%3d/%3d] %s FAILED: %s", i, len(ids), ident, e)
            failed += 1
        finally:
            signal.alarm(0)
        time.sleep(0.5)
    log.info("stock financials: %d obs written (%d fetched, %d failed, %d skipped)",
             total, done, failed, skipped)
    return total


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db-url",
                    default="postgresql+psycopg2://postgres:admin123@127.0.0.1:55432/postgres")
    ap.add_argument("--limit", type=int, default=None, help="cap stocks per shard (testing)")
    ap.add_argument("--shard", type=int, default=0, help="0-indexed shard id")
    ap.add_argument("--num-shards", type=int, default=1, help="total shard count")
    ap.add_argument("--no-resume-skip", action="store_true", help="don't skip already-crawled stocks")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    engine = create_engine(args.db_url)
    ids = load_stock_identifiers(engine)
    log.info("A-share identifier map: %d stocks", len(ids))
    if not ids:
        log.error("no akshare A-share identifiers found")
        return 1

    if args.num_shards > 1:
        ids = ids[args.shard::args.num_shards]
        log.info("shard %d/%d: %d stocks", args.shard, args.num_shards, len(ids))
    if args.limit:
        ids = ids[:args.limit]

    done_ids = set() if args.no_resume_skip else load_done_entity_ids(engine)
    log.info("resume-skip: %d stocks already have BS data", len(done_ids))

    bs_c = load_concepts(engine, list(BS_EM_COLS))
    ps_c = load_concepts(engine, list(PS_EM_COLS))
    cf_c = load_concepts(engine, list(CF_EM_COLS))
    log.info("concepts resolved: BS=%d PS=%d CF=%d", len(bs_c), len(ps_c), len(cf_c))

    if args.dry_run:
        for ident, eid in ids[:10]:
            log.info("  %s -> entity %d (done=%s)", ident, eid, eid in done_ids)
        log.info("(dry-run: %d stocks, %d to fetch, not writing)",
                 len(ids), len(ids) - sum(1 for _, e in ids if e in done_ids))
        return 0

    n = ingest(engine, ids, done_ids, bs_c, ps_c, cf_c, args.shard)
    log.info("done: %d observations upserted", n)
    return 0


if __name__ == "__main__":
    sys.exit(main())

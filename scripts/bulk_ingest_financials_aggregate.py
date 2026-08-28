"""Bulk-ingest A-share financials via AGGREGATE eastmoney endpoints.

The per-stock approach (stock_balance_sheet_by_report_em(symbol=...) × 5203
stocks × 3 statements = 15,609 calls) triggers eastmoney IP-level rate limiting
even at 4 concurrent pods — every stock hangs until the SIGALRM fires. A probe
proved the per-stock calls work fine from a SINGLE pod, but concurrency is the
killer.

The aggregate endpoints solve this: they return ALL ~5230 A-share stocks in a
single call per report-period, in ~5 seconds each:

    stock_zcfz_em(date="20241231")  -> 5230 rows × 15 cols (balance sheet)  ~4.5s
    stock_lrb_em(date="20241231")   -> 5230 rows × 15 cols (profit stmt)    ~4.5s
    stock_xjll_em(date="20241231")  -> 5230 rows × 12 cols (cash flow)      ~4.9s

Total: 3 calls/quarter × ~68 quarters = ~204 calls. Single-threaded, no
concurrency, no rate-limiting, no sharding. Runs as a single k8s Job pod with
direct in-cluster DB access (avoids port-forward write bottleneck).

Tradeoff: the aggregate endpoints expose fewer line items (~19 concepts vs 42
in the per-statement APIs). They cover the CORE financials though — total assets,
total liabilities, equity, revenue, net profit, operating profit, OCF/ICF/FCF —
for the entire universe in minutes. The detailed line items (goodwill, R&D, EPS,
income tax, etc.) can be backfilled per-stock later for index components if needed.

Concept mapping (verified from probe output):

  BS (stock_zcfz_em):           PS (stock_lrb_em):              CF (stock_xjll_em):
    资产-货币资金  -> BS_MON   净利润             -> PS_NET    净现金流-净现金流    -> CF_NET
    资产-应收账款  -> BS_AR    营业总收入          -> PS_REV    经营性现金流-净额     -> CF_OPR
    资产-存货      -> BS_INV   营业总支出-营业支出 -> PS_COST  投资性现金流-净额     -> CF_INV
    资产-总资产    -> BS_TA    营业总支出-销售费用  -> PS_SELL  融资性现金流-净额     -> CF_FIN
    负债-应付账款  -> BS_AP    营业总支出-管理费用  -> PS_ADMIN
    负债-总负债    -> BS_TL    营业总支出-财务费用  -> PS_FIN
    股东权益合计   -> BS_TE    营业利润             -> PS_OP
                                利润总额             -> PS_TP

The observation date = the report period (the ``date`` param), NOT 公告日期
(announcement date). Stocks that haven't reported for a period simply don't
appear in that period's DataFrame.
"""
from __future__ import annotations

import datetime
import logging
import signal
import socket
import sys
import time

from sqlalchemy import create_engine, text
from psycopg2.extras import execute_values

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("fin-agg")

ENTITY_TYPE = "stock"
SOURCE = "akshare"
# These frames are QUARTERLY report periods (0331/0630/0930/1231), so the rows
# must be tagged 'quarter'. `semantic_observations_read` partitions on
# (concept, entity_type, entity, date, granularity) — tagging a quarterly
# statement 'day' puts it in a different partition from the same fact stored
# correctly, so readers get both values instead of one.
GRANULARITY = "quarter"
SOCKET_TIMEOUT = 120  # per-recv safety net (aggregate calls take ~5s normally)
FETCH_ALARM = 200     # hard SIGALRM limit per aggregate fetch (kills trickle hangs)

# --------------------------------------------------------------------------
# Concept code -> aggregate Chinese column name.
# Keys are `concepts.code` for entity_type='stock' in the canonical DB; values
# are akshare frame columns (both verified against akshare 1.18.94 live).
#
# Every numeric line item the three aggregate frames expose is mapped. The
# remaining frame columns are 序号 / 股票简称 / 公告日期 and the *同比 / *占比
# year-over-year and ratio derivatives, which are computable from the levels
# and so are deliberately not stored.
# --------------------------------------------------------------------------
BS_AGG_COLS = {
    "financials.monetary_capital": "资产-货币资金",
    "financials.accounts_receivable": "资产-应收账款",
    "financials.inventory": "资产-存货",
    "financials.total_assets": "资产-总资产",
    "financials.accounts_payable": "负债-应付账款",
    "financials.total_liabilities": "负债-总负债",
    "financials.equity": "股东权益合计",
    "financials.debt_ratio": "资产负债率",
}

PS_AGG_COLS = {
    "financials.net_income": "净利润",
    "financials.revenue": "营业总收入",
    "financials.operating_cost": "营业总支出-营业支出",
    "financials.selling_expense": "营业总支出-销售费用",
    "financials.admin_expense": "营业总支出-管理费用",
    "financials.finance_expense": "营业总支出-财务费用",
    "financials.operating_profit": "营业利润",
    "financials.total_profit": "利润总额",
}

CF_AGG_COLS = {
    "financials.net_cash_flow": "净现金流-净现金流",
    "financials.operating_cash_flow": "经营性现金流-现金流量净额",
    "financials.investing_cash_flow": "投资性现金流-现金流量净额",
    "financials.financing_cash_flow": "融资性现金流-现金流量净额",
}

CODE_COL = "股票代码"  # stock code column in all three aggregate DataFrames


# --------------------------------------------------------------------------
# SIGALRM-bounded fetch — kills any aggregate call that hangs past FETCH_ALARM
# seconds (eastmoney occasionally leaves a socket open and trickles bytes
# forever; the 120s socket timeout catches dead sockets but not slow-trickle
# hangs, which stalled the pod at Q8 last run).
# --------------------------------------------------------------------------
def _fetch_with_alarm(fn, date_str: str, timeout: int = FETCH_ALARM):
    """Call an akshare aggregate fn(date=...) with a hard SIGALRM timeout."""
    def _handler(signum, frame):  # noqa: ARG001
        raise TimeoutError(f"{fn.__name__}({date_str}) exceeded {timeout}s")
    old = signal.signal(signal.SIGALRM, _handler)
    signal.alarm(timeout)
    try:
        return fn(date=date_str)
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------
def _fmt(v) -> str | None:
    """Coerce a cell to a clean numeric string; None for NaN/unparseable."""
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


def _iso(date_str: str) -> str:
    """'20241231' -> '2024-12-31'."""
    return f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}"


def _norm_code(raw) -> str | None:
    """Normalize a stock code cell to a bare 6-digit string."""
    if raw is None:
        return None
    s = str(raw).strip()
    # strip any prefix like SH/SZ/sh/sz
    if len(s) > 6 and s[:2].upper() in ("SH", "SZ"):
        s = s[2:]
    if not s.isdigit():
        return None
    return s.zfill(6)


def load_code_map(engine) -> dict[str, int]:
    """{bare_6digit_code: entity_id} for all A-share stocks."""
    with engine.connect() as c:
        rows = c.execute(text(
            "SELECT identifier, entity_id FROM entity_source_identifiers "
            "WHERE entity_type=:et AND source=:src"
        ), {"et": ENTITY_TYPE, "src": SOURCE}).all()
    out = {}
    for r in rows:
        code = _norm_code(r[0])
        if code is not None:
            out[code] = r[1]
    log.info("code map: %d stocks", len(out))
    return out


def load_concepts(engine, codes: list[str]) -> dict[str, dict]:
    if not codes:
        return {}
    with engine.connect() as c:
        rows = c.execute(text(
            "SELECT id, code, unit FROM concepts "
            "WHERE entity_type=:et AND code = ANY(:codes)"
        ), {"et": ENTITY_TYPE, "codes": codes}).all()
    return {r[1]: {"id": r[0], "unit": r[2]} for r in rows}


BATCH = 2000  # rows per transaction — small enough to survive port-forward stalls


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
    total = 0
    n_batches = (len(vals) + BATCH - 1) // BATCH
    for bi in range(0, len(vals), BATCH):
        chunk = vals[bi:bi + BATCH]
        last = None
        for attempt in range(5):
            try:
                with engine.begin() as conn:
                    cur = conn.connection.driver_connection.cursor()
                    try:
                        execute_values(cur, sql, chunk, page_size=500)
                    finally:
                        cur.close()
                total += len(chunk)
                break
            except Exception as e:  # noqa: BLE001
                last = e
                log.warning("upsert batch %d/%d attempt %d failed: %s",
                            bi // BATCH + 1, n_batches, attempt + 1, e)
                engine.dispose()
                time.sleep(2 * (attempt + 1))
        else:
            raise last
    return total


def extract_from_df(df, col_map, concepts, code_map, obs_date, now) -> list[dict]:
    """Extract observation rows from one aggregate DataFrame."""
    if df is None or df.empty or CODE_COL not in df.columns:
        return []
    rows = []
    for _, r in df.iterrows():
        code = _norm_code(r.get(CODE_COL))
        if code is None:
            continue
        eid = code_map.get(code)
        if eid is None:
            continue
        for ccode, col in col_map.items():
            meta = concepts.get(ccode)
            if meta is None:
                continue
            val = _fmt(r.get(col))
            if val is None:
                continue
            rows.append({
                "concept_id": meta["id"], "entity_type": ENTITY_TYPE,
                "entity_id": eid, "date": obs_date, "value": val,
                "unit": meta["unit"], "source_used": SOURCE,
                "fetched_at": now, "granularity": GRANULARITY,
            })
    return rows


def quarter_dates(start_year: int, end_year: int) -> list[str]:
    """Generate quarter-end dates 'YYYYMMDD' from start_year Q1 through end_year Q4."""
    dates = []
    for y in range(start_year, end_year + 1):
        for m, d in [(3, 31), (6, 30), (9, 30), (12, 31)]:
            dates.append(f"{y}{m:02d}{d:02d}")
    return dates


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--db-url",
                    default="postgresql+psycopg2://postgres:admin123@127.0.0.1:55432/postgres")
    ap.add_argument("--start-year", type=int, default=2015)
    ap.add_argument("--end-year", type=int, default=2026)
    args = ap.parse_args()

    socket.setdefaulttimeout(SOCKET_TIMEOUT)

    engine = create_engine(args.db_url,
                           connect_args={"connect_timeout": 10},
                           pool_pre_ping=True,
                           pool_recycle=300)
    code_map = load_code_map(engine)
    if not code_map:
        log.error("no stock identifiers found — aborting")
        return 1

    all_codes = list(BS_AGG_COLS) + list(PS_AGG_COLS) + list(CF_AGG_COLS)
    concepts = load_concepts(engine, all_codes)
    log.info("concepts resolved: %d/%d", len(concepts), len(all_codes))
    missing = [c for c in all_codes if c not in concepts]
    if missing:
        log.warning("MISSING concepts: %s", missing)

    import akshare as ak
    dates = quarter_dates(args.start_year, args.end_year)
    log.info("fetching %d quarters (%s..%s)", len(dates), dates[0], dates[-1])

    now = datetime.datetime.now(datetime.timezone.utc)
    grand_total = 0
    for i, date_str in enumerate(dates, 1):
        obs_date = _iso(date_str)
        rows = []
        for label, fn, col_map in [
            ("BS", ak.stock_zcfz_em, BS_AGG_COLS),
            ("PS", ak.stock_lrb_em, PS_AGG_COLS),
            ("CF", ak.stock_xjll_em, CF_AGG_COLS),
        ]:
            t0 = time.time()
            try:
                df = _fetch_with_alarm(fn, date_str)
                dt = time.time() - t0
                n_stocks = len(df) if df is not None else 0
                extracted = extract_from_df(df, col_map, concepts, code_map, obs_date, now)
                rows.extend(extracted)
                log.info("[%2d/%d] %s %s: %d stocks -> %d obs (%.1fs)",
                         i, len(dates), label, date_str, n_stocks, len(extracted), dt)
            except Exception as e:  # noqa: BLE001
                log.warning("[%2d/%d] %s %s FAILED (%.1fs): %s",
                            i, len(dates), label, date_str, time.time() - t0, e)
            time.sleep(0.3)
        if rows:
            n = upsert(engine, rows)
            grand_total += n
            log.info("[%2d/%d] %s: upserted %d obs (cumulative %d)",
                     i, len(dates), date_str, n, grand_total)
        else:
            log.info("[%2d/%d] %s: no rows (skipped)", i, len(dates), date_str)
    log.info("=== done: %d total observations upserted ===", grand_total)
    # yield accounting (fix-silent-zero-yield-crawls): report so the run row's
    # D3 classification reflects landed data instead of reading zero_yield
    try:
        from fd_open_data_mcp.refresh.yield_report import report_run_yield
        report_run_yield(grand_total, grand_total)
    except Exception:  # noqa: BLE001 - accounting must never fail the ingest
        log.warning("yield report skipped", exc_info=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

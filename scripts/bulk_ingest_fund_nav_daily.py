"""Bulk-ingest open-end fund daily NAV via the eastmoney aggregate endpoint.

``fund_open_fund_daily_em()`` returns EVERY open-end fund (~23,900 rows) in a
single ~3s call, and carries TWO trading days of NAV per fund. One call per day
therefore covers the whole universe with no per-fund fan-out — the per-fund
history endpoints live on ``push2his.eastmoney.com``, which is unreachable from
the crawl clusters.

The NAV columns are DATE-PREFIXED and shift every trading day:

    ['基金代码', '基金简称',
     '2026-08-24-单位净值', '2026-08-24-累计净值',
     '2026-08-21-单位净值', '2026-08-21-累计净值',
     '日增长值', '日增长率', '申购状态', '赎回状态', '手续费']

so the observation date is parsed OUT of the column name rather than assumed to
be "today" — a run on a Monday backfills the preceding Friday for free, and a
run that happens after a data-vendor delay still dates its rows correctly.

``日增长率`` has no date prefix; it describes the LATEST of the two dates only.
"""
from __future__ import annotations

import datetime
import logging
import re
import signal
import socket
import sys
import time

from psycopg2.extras import execute_values
from sqlalchemy import create_engine, text

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("fund-nav")

ENTITY_TYPE = "fund"
SOURCE = "akshare"
GRANULARITY = "day"
SOCKET_TIMEOUT = 120
FETCH_ALARM = 200
BATCH = 2000

CODE_COL = "基金代码"
GROWTH_COL = "日增长率"

# Date-prefixed NAV column suffix -> concept code.
NAV_SUFFIX_CONCEPTS = {
    "单位净值": "nav.unit",
    "累计净值": "nav.accumulated",
}
# Undated column -> concept code; applies to the latest date in the frame.
LATEST_ONLY_CONCEPTS = {GROWTH_COL: "nav.daily_growth"}

# '2026-08-24-单位净值' -> ('2026-08-24', '单位净值')
_DATED_COL = re.compile(r"^(\d{4}-\d{2}-\d{2})-(.+)$")


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


def _norm_code(raw) -> str | None:
    """Fund codes are 6 digits; eastmoney and the registry disagree on padding."""
    if raw is None:
        return None
    s = str(raw).strip()
    if not s or not s.isdigit():
        return None
    return s.zfill(6)


def _fetch_with_alarm(fn, timeout: int = FETCH_ALARM):
    """eastmoney occasionally trickles bytes forever; the socket timeout catches
    dead sockets but not slow-trickle hangs, so bound the whole call."""
    def _handler(signum, frame):  # noqa: ARG001
        raise TimeoutError(f"fetch exceeded {timeout}s")

    signal.signal(signal.SIGALRM, _handler)
    signal.alarm(timeout)
    try:
        return fn()
    finally:
        signal.alarm(0)


def load_code_map(engine) -> dict[str, int]:
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
    log.info("code map: %d funds", len(out))
    return out


def load_concepts(engine, codes: list[str]) -> dict[str, dict]:
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


def plan_columns(df) -> tuple[dict[str, list[tuple[str, str]]], str | None]:
    """Group the frame's dated NAV columns by observation date.

    Returns ({date: [(column, concept_code), ...]}, latest_date).
    """
    by_date: dict[str, list[tuple[str, str]]] = {}
    for col in df.columns:
        m = _DATED_COL.match(str(col))
        if not m:
            continue
        date, suffix = m.group(1), m.group(2)
        code = NAV_SUFFIX_CONCEPTS.get(suffix)
        if code:
            by_date.setdefault(date, []).append((col, code))
    latest = max(by_date) if by_date else None
    return by_date, latest


def extract(df, by_date, latest, concepts, code_map, now) -> list[dict]:
    rows: list[dict] = []
    seen_codes = 0
    for rec in df.to_dict("records"):
        entity_id = code_map.get(_norm_code(rec.get(CODE_COL)))
        if entity_id is None:
            continue
        seen_codes += 1
        for date, pairs in by_date.items():
            for col, concept_code in pairs:
                meta = concepts.get(concept_code)
                if meta is None:
                    continue
                value = _fmt(rec.get(col))
                if value is None:
                    continue
                rows.append({
                    "concept_id": meta["id"], "entity_type": ENTITY_TYPE,
                    "entity_id": entity_id, "date": date, "value": value,
                    "unit": meta["unit"], "source_used": SOURCE,
                    "fetched_at": now, "granularity": GRANULARITY,
                })
        if latest:
            for col, concept_code in LATEST_ONLY_CONCEPTS.items():
                meta = concepts.get(concept_code)
                if meta is None:
                    continue
                value = _fmt(rec.get(col))
                if value is None:
                    continue
                rows.append({
                    "concept_id": meta["id"], "entity_type": ENTITY_TYPE,
                    "entity_id": entity_id, "date": latest, "value": value,
                    "unit": meta["unit"], "source_used": SOURCE,
                    "fetched_at": now, "granularity": GRANULARITY,
                })
    log.info("matched %d/%d funds in the registry -> %d obs",
             seen_codes, len(df), len(rows))
    return rows


def selfcheck() -> int:
    """Assert the column-date parsing and extraction on a synthetic frame."""
    import pandas as pd

    df = pd.DataFrame([
        {CODE_COL: "17581", "基金简称": "A", "2026-08-24-单位净值": "1.0829",
         "2026-08-24-累计净值": "1.0829", "2026-08-21-单位净值": "1.08",
         "2026-08-21-累计净值": "1.08", GROWTH_COL: "4.80", "手续费": "0.04%"},
        {CODE_COL: "999999", "基金简称": "unregistered", "2026-08-24-单位净值": "2.0",
         "2026-08-24-累计净值": "2.0", "2026-08-21-单位净值": "2.0",
         "2026-08-21-累计净值": "2.0", GROWTH_COL: "---", "手续费": "-"},
    ])

    by_date, latest = plan_columns(df)
    assert sorted(by_date) == ["2026-08-21", "2026-08-24"], by_date
    assert latest == "2026-08-24", latest
    # 手续费/基金简称 carry no date prefix -> never collected
    assert all(len(p) == 2 for p in by_date.values()), by_date

    concepts = {"nav.unit": {"id": 378, "unit": "currency_cny"},
                "nav.accumulated": {"id": 379, "unit": "currency_cny"},
                "nav.daily_growth": {"id": 380, "unit": "%"}}
    now = datetime.datetime(2026, 8, 25, tzinfo=datetime.timezone.utc)
    # only 017581 is registered; note the frame's unpadded '17581'
    rows = extract(df, by_date, latest, concepts, {"017581": 42}, now)

    assert {r["entity_id"] for r in rows} == {42}, "unregistered fund leaked"
    # 2 dates x 2 nav concepts + 1 growth on the latest date only
    assert len(rows) == 5, len(rows)
    growth = [r for r in rows if r["concept_id"] == 380]
    assert len(growth) == 1 and growth[0]["date"] == "2026-08-24", growth
    assert all(r["granularity"] == GRANULARITY for r in rows)
    assert _fmt("---") is None and _fmt("1.0") == "1"
    assert _norm_code("17581") == "017581" and _norm_code("-") is None

    print("selfcheck OK")
    return 0


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--db-url")
    ap.add_argument("--selfcheck", action="store_true")
    args = ap.parse_args()

    if args.selfcheck:
        return selfcheck()
    if not args.db_url:
        ap.error("--db-url is required unless --selfcheck")

    socket.setdefaulttimeout(SOCKET_TIMEOUT)
    engine = create_engine(args.db_url, connect_args={"connect_timeout": 10},
                           pool_pre_ping=True, pool_recycle=300)

    code_map = load_code_map(engine)
    if not code_map:
        log.error("no fund identifiers found — aborting")
        return 1

    wanted = list(NAV_SUFFIX_CONCEPTS.values()) + list(LATEST_ONLY_CONCEPTS.values())
    concepts = load_concepts(engine, wanted)
    log.info("concepts resolved: %d/%d", len(concepts), len(wanted))
    missing = [c for c in wanted if c not in concepts]
    if missing:
        log.warning("MISSING concepts: %s", missing)
    if not concepts:
        log.error("no concepts resolved — aborting")
        return 1

    import akshare as ak
    t0 = time.time()
    df = _fetch_with_alarm(ak.fund_open_fund_daily_em)
    log.info("fetched %d funds in %.1fs", len(df), time.time() - t0)

    by_date, latest = plan_columns(df)
    if not by_date:
        log.error("no dated NAV columns found in %s — endpoint shape changed",
                  list(df.columns))
        return 1
    log.info("dates in frame: %s (latest=%s)", sorted(by_date), latest)

    now = datetime.datetime.now(datetime.timezone.utc)
    rows = extract(df, by_date, latest, concepts, code_map, now)
    n = upsert(engine, rows)
    log.info("=== done: %d observations upserted ===", n)
    # yield accounting (fix-silent-zero-yield-crawls): report so the run row's
    # D3 classification reflects landed data instead of reading zero_yield
    try:
        from fd_open_data_mcp.refresh.yield_report import report_run_yield
        report_run_yield(n, n)
    except Exception:  # noqa: BLE001 - accounting must never fail the ingest
        log.warning("yield report skipped", exc_info=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

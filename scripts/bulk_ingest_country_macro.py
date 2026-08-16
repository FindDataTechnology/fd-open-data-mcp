"""Bulk-ingest China macro indicators into semantic_observations.

The country ontology has 111 `country` concepts (China/US/EU/JP macro + World Bank
WDI) but ZERO `country` entities and ZERO observations — this is the biggest
uncrawled "valuable data" gap after the stock/ETF OHLCV backfills. This script
closes the China half of it: seed the `CN` entity and backfill the core China
macro frame (growth / inflation / money supply / PMI / trade / investment /
consumption / LPR) in one pass.

Source: akshare `macro_china_*` (eastmoney / NBS / 金十, one call per indicator)
  -> monthly reference periods (东方财富 style) for most indicators; quarterly for
     GDP; release-dated (金十 style) series are deliberately avoided.

Dates are normalized to the ontology convention (see semantic_observations):
  monthly  -> granularity "month", date "YYYY-MM-01"
  quarterly-> granularity "day",   date quarter-end "YYYY-MM-DD" (the planner's
              `_granularity_for` falls quarterly through to "day"; we match it)
Re-running is idempotent (ON CONFLICT by concept/entity/date/granularity).

Usage (scraw-fd-open-data-mcp venv has akshare):
    /Users/chengsishi/finddata/scraw-fd-open-data-mcp/.venv/bin/python \
        scripts/bulk_ingest_country_macro.py [--db-url URL] [--dry-run]
"""
from __future__ import annotations

import argparse
import datetime
import logging
import re
import sys
import time

from sqlalchemy import create_engine, text
from psycopg2.extras import execute_values

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("country-macro-ingest")

ENTITY_TYPE = "country"
SOURCE = "akshare"
COUNTRY_CODE = "CN"

# concept code -> (akshare fn, dataframe column). unit + frequency come from the
# concepts table; granularity is derived from frequency.
#
#   "month":    reference period is a month (eastmoney `月份` / `统计时间`)
#   "quarter":  reference period is a quarter (`季度`)
#   "release":  reference period is a release date (金十 `日期`) -- AVOIDED below
CN_MACRO_MAP: dict[str, tuple[str, str, str]] = {
    # code            (akshare fn,                      column,                    cadence)
    "GDP_AMOUNT":     ("macro_china_gdp",               "国内生产总值-绝对值",       "quarter"),
    "GDP_YOY":        ("macro_china_gdp",               "国内生产总值-同比增长",     "quarter"),
    "CPI_YOY":        ("macro_china_cpi",               "全国-同比增长",             "month"),
    "CPI_MOM":        ("macro_china_cpi",               "全国-环比增长",             "month"),
    "PPI_YOY":        ("macro_china_ppi",               "当月同比增长",              "month"),
    "PMI_MFG":        ("macro_china_pmi",               "制造业-指数",               "month"),
    "PMI_NONMFG":     ("macro_china_pmi",               "非制造业-指数",             "month"),
    "M0_AMOUNT":      ("macro_china_supply_of_money",   "流通中现金(M0)",            "month"),
    "M0_YOY":         ("macro_china_supply_of_money",   "流通中现金(M0)同比增长",     "month"),
    "M1_AMOUNT":      ("macro_china_supply_of_money",   "货币(狭义货币M1)",           "month"),
    "M1_YOY":         ("macro_china_supply_of_money",   "货币(狭义货币M1)同比增长",   "month"),
    "M2_AMOUNT":      ("macro_china_supply_of_money",   "货币和准货币（广义货币M2）", "month"),
    "M2_YOY":         ("macro_china_supply_of_money",   "货币和准货币（广义货币M2）同比增长", "month"),
    "EXPORT_YOY":     ("macro_china_hgjck",             "当月出口额-同比增长",       "month"),
    "IMPORT_YOY":     ("macro_china_hgjck",             "当月进口额-同比增长",       "month"),
    "FAI_CUMULATIVE": ("macro_china_gdzctz",            "自年初累计",                "month"),
    "FDI_AMOUNT":     ("macro_china_fdi",               "累计",                      "month"),
    "RETAIL_AMOUNT":  ("macro_china_consumer_goods_retail", "当月",                 "month"),
    "RETAIL_YOY":     ("macro_china_consumer_goods_retail", "同比增长",             "month"),
    "IND_PROD_YOY":   ("macro_china_gyzjz",             "同比增长",                  "month"),
    "LPR_1Y":         ("macro_china_lpr",               "LPR1Y",                     "month"),
    "LPR_5Y":         ("macro_china_lpr",               "LPR5Y",                     "month"),
    "RESERVE_RATIO_LARGE": ("macro_china_reserve_requirement_ratio", "大型金融机构-调整后", "event"),
    "ELECTRICITY":    ("macro_china_society_electricity", "全社会用电量",             "month"),
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


_CN_MONTH_RE = re.compile(r"(\d{4})年(\d{1,2})月份?")
_DOT_YM_RE = re.compile(r"(\d{4})\.(\d{1,2})")
_CN_QUARTER_RE = re.compile(r"(\d{4})年第1(?:-(\d))?季度")
_CN_DATE_RE = re.compile(r"(\d{4})年(\d{1,2})月(\d{1,2})日")


def _month_date(s) -> str | None:
    """Month reference period -> canonical 'YYYY-MM-01' (or None).

    Handles Chinese month strings ('2008年01月份'), dot YM ('2026.6'), and
    datetime.date/datetime objects (akshare `TRADE_DATE` in macro_china_lpr).
    """
    if isinstance(s, (datetime.datetime, datetime.date)):
        return f"{s.year:04d}-{s.month:02d}-01"
    s = str(s).strip()
    m = _CN_MONTH_RE.match(s)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-01"
    m = _DOT_YM_RE.match(s)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-01"
    return None


def _quarter_date(s) -> str | None:
    """'2006年第1季度' -> '2006-03-31'; cumulative '2025年第1-4季度' -> '2025-12-31'."""
    m = _CN_QUARTER_RE.search(str(s))
    if not m:
        return None
    y = int(m.group(1))
    q = int(m.group(2)) if m.group(2) else 1
    end = {1: "03-31", 2: "06-30", 3: "09-30", 4: "12-31"}[q]
    return f"{y}-{end}"


def _event_date(s) -> str | None:
    """'2007年01月05日' -> month-start '2007-01-01' (reserve ratio is monthly)."""
    m = _CN_DATE_RE.search(str(s))
    if not m:
        return None
    return f"{m.group(1)}-{int(m.group(2)):02d}-01"


def _trade_balance_row(r, eid, meta, now, granularity, d):
    """EXPORT - IMPORT from macro_china_hgjck's two amount columns."""
    exp = _fmt(r.get("当月出口额-金额"))
    imp = _fmt(r.get("当月进口额-金额"))
    if exp is None or imp is None:
        return None
    return {
        "concept_id": meta["id"], "entity_type": ENTITY_TYPE, "entity_id": eid,
        "date": d, "value": _fmt(float(exp) - float(imp)), "unit": meta["unit"],
        "source_used": SOURCE, "fetched_at": now, "granularity": granularity,
    }


def upsert(engine, rows: list[dict]) -> int:
    if not rows:
        return 0
    # Collapse duplicate (concept, entity, date, granularity) keys — keep the last
    # row (latest change wins). Prevents CardinalityViolation when an indicator has
    # multiple values in one reference period (e.g. reserve ratio changed 2x/month).
    dedup: dict[tuple, dict] = {}
    for r in rows:
        key = (r["concept_id"], r["entity_type"], r["entity_id"], r["date"], r["granularity"])
        dedup[key] = r
    rows = list(dedup.values())
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
            engine.dispose()
            time.sleep(2 * (attempt + 1))
    raise last


def seed_country(engine) -> int:
    """Upsert the CN country entity, return its id."""
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO entities (entity_type, code, name_zh, name_en) "
            "VALUES (:et, :code, :zh, :en) "
            "ON CONFLICT (entity_type, code) DO UPDATE SET name_zh=EXCLUDED.name_zh, "
            "name_en=EXCLUDED.name_en"
        ), {"et": ENTITY_TYPE, "code": COUNTRY_CODE, "zh": "中国", "en": "China"})
    with engine.connect() as c:
        eid = c.execute(text(
            "SELECT id FROM entities WHERE entity_type=:et AND code=:code"
        ), {"et": ENTITY_TYPE, "code": COUNTRY_CODE}).scalar()
    return eid


def load_concepts(engine) -> dict[str, dict]:
    codes = list(CN_MACRO_MAP) + ["TRADE_BALANCE"]
    with engine.connect() as c:
        rows = c.execute(text(
            "SELECT code, id, unit, frequency FROM concepts "
            "WHERE entity_type=:et AND code = ANY(:codes)"
        ), {"et": ENTITY_TYPE, "codes": codes}).all()
    return {r[0]: {"id": r[1], "unit": r[2], "frequency": r[3]} for r in rows}


def _granularity(frequency: str | None) -> str:
    if frequency == "yearly":
        return "year"
    if frequency == "monthly":
        return "month"
    return "day"


def ingest(engine, eid, concepts) -> tuple[int, int, int]:
    import akshare as ak

    now = datetime.datetime.now(datetime.timezone.utc)
    total = failed = 0
    for code, (fn_name, col, cadence) in CN_MACRO_MAP.items():
        meta = concepts.get(code)
        if meta is None:
            log.info("skip %s: concept missing", code)
            continue
        fn = getattr(ak, fn_name, None)
        if fn is None:
            log.info("skip %s: akshare has no %s", code, fn_name)
            continue
        try:
            df = fn()
        except Exception as e:  # noqa: BLE001
            failed += 1
            log.info("%-20s FAILED: %s", code, e)
            continue
        if df is None or df.empty:
            failed += 1
            log.info("%-20s empty", code)
            continue

        granularity = _granularity(meta["frequency"])
        rows: list[dict] = []
        date_col = _date_col_for(fn_name)
        for _, r in df.iterrows():
            raw = r.get(date_col)
            if cadence == "quarter":
                d = _quarter_date(raw)
            elif cadence == "event":
                d = _event_date(raw)
            else:
                d = _month_date(raw)
            if d is None:
                continue
            val = _fmt(r.get(col))
            if val is None:
                continue
            rows.append({
                "concept_id": meta["id"], "entity_type": ENTITY_TYPE, "entity_id": eid,
                "date": d, "value": val, "unit": meta["unit"],
                "source_used": SOURCE, "fetched_at": now, "granularity": granularity,
            })

        # TRADE_BALANCE is derived from the same hgjck frame.
        if fn_name == "macro_china_hgjck":
            tb = concepts.get("TRADE_BALANCE")
            if tb is not None:
                for _, r in df.iterrows():
                    d = _month_date(r.get(date_col))
                    if d is None:
                        continue
                    row = _trade_balance_row(r, eid, tb, now, "month", d)
                    if row:
                        rows.append(row)

        n = upsert(engine, rows)
        total += n
        log.info("%-20s %d obs (granularity=%s)", code, n, granularity)
        time.sleep(0.15)
    log.info("country macro: %d obs written, %d indicators failed", total, failed)
    return total, failed, 0


def _date_col_for(fn_name: str) -> str:
    return {
        "macro_china_gdp": "季度",
        "macro_china_supply_of_money": "统计时间",
        "macro_china_reserve_requirement_ratio": "生效时间",
        "macro_china_lpr": "TRADE_DATE",
        "macro_china_society_electricity": "统计时间",
    }.get(fn_name, "月份")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db-url",
                    default="postgresql+psycopg2://postgres:admin123@127.0.0.1:55432/postgres")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    engine = create_engine(args.db_url)
    eid = seed_country(engine)
    log.info("country entity CN = id %d", eid)

    concepts = load_concepts(engine)
    log.info("concepts resolved: %s", ", ".join(concepts))

    if args.dry_run:
        for code, (fn_name, col, cadence) in CN_MACRO_MAP.items():
            log.info("  %-20s <- %s.%s (%s)", code, fn_name, col, cadence)
        return 0

    total, failed, _ = ingest(engine, eid, concepts)
    log.info("done: %d observations upserted (%d failed)", total, failed)
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Bulk-ingest US/EU/JP (G7) macro indicators into semantic_observations.

Companion to bulk_ingest_country_macro.py (which closed the China half). This one
seeds the US / EU / JP `country` entities and backfills the ~23 US_*/EU_*/JP_*
concepts that akshare's 金十/eastmoney `macro_usa_*` / `macro_euro_*` /
`macro_japan_*` / `macro_bank_*` functions expose, plus the CN central-bank rate.

Schemas (inspected live against akshare 1.18.83):
  macro_usa_* / macro_euro_* / macro_bank_*  -> ['商品','日期','今值','预测值','前值']
      日期 is a datetime.date (month-start for reference-month series, a release
      date for the 金十 release-dated ones like non_farm / ppi / gdp_monthly /
      interest rates); 今值 is the value.
  macro_usa_cpi_yoy / macro_japan_*          -> ['时间','前值','现值','发布日期']
      时间 is "2008年01月"; 现值 is the value.

Date normalization:
  cadence "month" -> snap any date to "YYYY-MM-01", granularity "month".
      (release-dated series are stored under their release month — a documented
      approximation; the 金十 series are "latest value as of that date" anyway.)
  cadence "day"   -> keep the actual date "YYYY-MM-DD", granularity "day"
      (used for irregular central-bank interest-rate decisions).

Not covered here (no akshare source): JP_GDP_YOY, JP_INDUSTRIAL_PROD, and the
World Bank WDI (WB_*) concepts — those need the wbgapi/datacommons path.

Usage (scraw-fd-open-data-mcp venv has akshare):
    /Users/chengsishi/finddata/scraw-fd-open-data-mcp/.venv/bin/python \
        scripts/bulk_ingest_country_macro_g7.py [--db-url URL] [--dry-run]
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
log = logging.getLogger("g7-macro-ingest")

ENTITY_TYPE = "country"
SOURCE = "akshare"

ENTITIES = {
    "US": ("美国", "United States"),
    "EU": ("欧盟", "European Union"),
    "JP": ("日本", "Japan"),
    "CN": ("中国", "China"),
}

# concept code -> (entity code, akshare fn, date col, value col, cadence)
G7_MACRO_MAP: dict[str, tuple[str, str, str, str, str]] = {
    # --- United States ---
    "US_CPI_YOY":            ("US", "macro_usa_cpi_yoy",               "时间", "现值", "month"),
    "US_CPI_MOM":            ("US", "macro_usa_cpi_monthly",           "日期", "今值", "month"),
    "US_CORE_CPI":           ("US", "macro_usa_core_cpi_monthly",      "日期", "今值", "month"),
    "US_PPI":                ("US", "macro_usa_ppi",                   "日期", "今值", "month"),
    "US_ISM_PMI":            ("US", "macro_usa_ism_pmi",               "日期", "今值", "month"),
    "US_RETAIL_SALES":       ("US", "macro_usa_retail_sales",          "日期", "今值", "month"),
    "US_TRADE_BALANCE":      ("US", "macro_usa_trade_balance",         "日期", "今值", "month"),
    "US_HOUSE_STARTS":       ("US", "macro_usa_house_starts",          "日期", "今值", "month"),
    "US_HOUSE_PRICE":        ("US", "macro_usa_house_price_index",     "日期", "今值", "month"),
    "US_CONSUMER_CONFIDENCE": ("US", "macro_usa_cb_consumer_confidence", "日期", "今值", "month"),
    "US_GDP_YOY":            ("US", "macro_usa_gdp_monthly",           "日期", "今值", "month"),
    "US_INDUSTRIAL_PROD":    ("US", "macro_usa_industrial_production", "日期", "今值", "month"),
    "US_NONFARM":            ("US", "macro_usa_non_farm",              "日期", "今值", "month"),
    "US_UNEMPLOYMENT":       ("US", "macro_usa_unemployment_rate",     "日期", "今值", "month"),
    "US_INTEREST_RATE":      ("US", "macro_bank_usa_interest_rate",    "日期", "今值", "day"),
    # --- Eurozone ---
    "EU_CPI_YOY":            ("EU", "macro_euro_cpi_yoy",              "日期", "今值", "month"),
    "EU_GDP_YOY":            ("EU", "macro_euro_gdp_yoy",              "日期", "今值", "month"),
    "EU_UNEMPLOYMENT":       ("EU", "macro_euro_unemployment_rate_mom", "日期", "今值", "month"),
    "EU_PMI_MFG":            ("EU", "macro_euro_manufacturing_pmi",    "日期", "今值", "month"),
    "EU_INTEREST_RATE":      ("EU", "macro_bank_euro_interest_rate",   "日期", "今值", "day"),
    # --- Japan ---
    "JP_CPI_YOY":            ("JP", "macro_japan_cpi_yearly",          "时间", "现值", "month"),
    "JP_UNEMPLOYMENT":       ("JP", "macro_japan_unemployment_rate",   "时间", "现值", "month"),
    "JP_INTEREST_RATE":      ("JP", "macro_bank_japan_interest_rate",  "日期", "今值", "day"),
    # --- China central-bank rate (bonus) ---
    "BANK_INTEREST":         ("CN", "macro_bank_china_interest_rate",  "日期", "今值", "day"),
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


def _month_date(s) -> str | None:
    """Any date -> canonical 'YYYY-MM-01' (month granularity)."""
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


def _day_date(s) -> str | None:
    """Actual date -> 'YYYY-MM-DD' (irregular/day granularity)."""
    if isinstance(s, (datetime.datetime, datetime.date)):
        return f"{s.year:04d}-{s.month:02d}-{s.day:02d}"
    s = str(s).strip()
    m = re.search(r"(\d{4})年(\d{1,2})月(\d{1,2})日", s)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}"
    return None


def upsert(engine, rows: list[dict]) -> int:
    if not rows:
        return 0
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


def seed_entities(engine) -> dict[str, int]:
    ids = {}
    for code, (zh, en) in ENTITIES.items():
        with engine.begin() as conn:
            conn.execute(text(
                "INSERT INTO entities (entity_type, code, name_zh, name_en) "
                "VALUES (:et, :code, :zh, :en) "
                "ON CONFLICT (entity_type, code) DO UPDATE SET name_zh=EXCLUDED.name_zh, "
                "name_en=EXCLUDED.name_en"
            ), {"et": ENTITY_TYPE, "code": code, "zh": zh, "en": en})
    with engine.connect() as c:
        for code in ENTITIES:
            ids[code] = c.execute(text(
                "SELECT id FROM entities WHERE entity_type=:et AND code=:code"
            ), {"et": ENTITY_TYPE, "code": code}).scalar()
    return ids


def load_concepts(engine) -> dict[str, dict]:
    codes = list(G7_MACRO_MAP)
    with engine.connect() as c:
        rows = c.execute(text(
            "SELECT code, id, unit, frequency FROM concepts "
            "WHERE entity_type=:et AND code = ANY(:codes)"
        ), {"et": ENTITY_TYPE, "codes": codes}).all()
    return {r[0]: {"id": r[1], "unit": r[2], "frequency": r[3]} for r in rows}


def ingest(engine, eids, concepts) -> tuple[int, int]:
    import akshare as ak

    now = datetime.datetime.now(datetime.timezone.utc)
    total = failed = 0
    for code, (ent, fn_name, date_col, val_col, cadence) in G7_MACRO_MAP.items():
        meta = concepts.get(code)
        if meta is None:
            log.info("skip %-22s concept missing", code)
            continue
        eid = eids.get(ent)
        if eid is None:
            log.info("skip %-22s entity %s missing", code, ent)
            continue
        fn = getattr(ak, fn_name, None)
        if fn is None:
            log.info("skip %-22s akshare has no %s", code, fn_name)
            continue
        try:
            df = fn()
        except Exception as e:  # noqa: BLE001
            failed += 1
            log.info("%-22s FAILED: %s", code, e)
            continue
        if df is None or df.empty:
            failed += 1
            log.info("%-22s empty", code)
            continue

        granularity = "month" if cadence == "month" else "day"
        normalizer = _month_date if cadence == "month" else _day_date
        rows: list[dict] = []
        for _, r in df.iterrows():
            d = normalizer(r.get(date_col))
            if d is None:
                continue
            val = _fmt(r.get(val_col))
            if val is None:
                continue
            rows.append({
                "concept_id": meta["id"], "entity_type": ENTITY_TYPE, "entity_id": eid,
                "date": d, "value": val, "unit": meta["unit"],
                "source_used": SOURCE, "fetched_at": now, "granularity": granularity,
            })

        n = upsert(engine, rows)
        total += n
        log.info("%-22s %-30s %d obs (%s, gran=%s)", code, fn_name, n, ent, granularity)
        time.sleep(0.15)
    log.info("g7 macro: %d obs written, %d indicators failed", total, failed)
    return total, failed


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db-url",
                    default="postgresql+psycopg2://postgres:admin123@127.0.0.1:55432/postgres")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    engine = create_engine(args.db_url)
    eids = seed_entities(engine)
    log.info("entities: %s", {k: v for k, v in eids.items()})

    concepts = load_concepts(engine)
    log.info("concepts resolved: %d/%d", len(concepts), len(G7_MACRO_MAP))

    if args.dry_run:
        for code, (ent, fn_name, date_col, val_col, cadence) in G7_MACRO_MAP.items():
            log.info("  %-22s <- %s.%s (%s/%s/%s)", code, fn_name, val_col, ent, date_col, cadence)
        return 0

    total, failed = ingest(engine, eids, concepts)
    log.info("done: %d observations upserted (%d failed)", total, failed)
    return 0


if __name__ == "__main__":
    sys.exit(main())

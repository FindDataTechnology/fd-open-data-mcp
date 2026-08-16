"""Bulk-ingest the remaining China misc macro concepts into semantic_observations.

Closes the last "empty but crawlable" country concepts left over after the three
main backfills (China macro, G7 macro, World Bank WDI). These are the near-
duplicate / lower-priority China indicators that the `macro_china_*` functions
expose but the primary `bulk_ingest_country_macro.py` pass did not fill:

  CPI_MONTHLY        <- macro_china_cpi                     "全国-环比增长"
  ELECTRICITY_TOTAL  <- macro_china_society_electricity     "全社会用电量"
  FAI_MONTHLY        <- macro_china_gdzctz                  "当月"
  RESERVE_RATIO      <- macro_china_reserve_requirement_ratio "大型金融机构-调整后"
  RETAIL_CUMULATIVE  <- macro_china_consumer_goods_retail   "累计"
  RETAIL_MONTHLY     <- macro_china_consumer_goods_retail   "当月"
  UNEMPLOYMENT_CN    <- macro_china_urban_unemployment      item=="全国城镇调查失业率"
  NEW_HOUSE_PRICE    <- macro_china_new_house_price         avg(上海,北京) 新建-同比
  USED_HOUSE_PRICE   <- macro_china_new_house_price         avg(上海,北京) 二手-同比

Notes:
  * CPI_MONTHLY duplicates CPI_MOM, RETAIL_MONTHLY duplicates RETAIL_AMOUNT,
    RESERVE_RATIO duplicates RESERVE_RATIO_LARGE, ELECTRICITY_TOTAL duplicates
    ELECTRICITY — these are distinct concept rows in the ontology that the primary
    pass left empty, so we backfill them with the same underlying column.
  * NEW/USED_HOUSE_PRICE: akshare's macro_china_new_house_price only exposes the
    two tier-1 cities 上海 + 北京 (no national row). We store the per-date mean of
    their YoY indices as a tier-1 proxy — a documented approximation.

Usage (scraw-fd-open-data-mcp venv has akshare):
    /Users/chengsishi/finddata/scraw-fd-open-data-mcp/.venv/bin/python \
        scripts/bulk_ingest_country_misc.py [--db-url URL] [--dry-run]
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
log = logging.getLogger("misc-macro-ingest")

ENTITY_TYPE = "country"
SOURCE = "akshare"

CONCEPTS = [
    "CPI_MONTHLY", "ELECTRICITY_TOTAL", "FAI_MONTHLY", "RESERVE_RATIO",
    "RETAIL_CUMULATIVE", "RETAIL_MONTHLY", "UNEMPLOYMENT_CN",
    "NEW_HOUSE_PRICE", "USED_HOUSE_PRICE",
]


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
_CN_DATE_RE = re.compile(r"(\d{4})年(\d{1,2})月(\d{1,2})日")
_YM_RE = re.compile(r"(\d{4})(\d{2})")


def _month_date(s) -> str | None:
    if isinstance(s, (datetime.datetime, datetime.date)):
        return f"{s.year:04d}-{s.month:02d}-01"
    s = str(s).strip()
    m = _CN_MONTH_RE.match(s)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-01"
    m = _DOT_YM_RE.match(s)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-01"
    m = _YM_RE.match(s)  # '201801'
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}-01"
    return None


def _event_date(s) -> str | None:
    m = _CN_DATE_RE.search(str(s))
    if not m:
        return None
    return f"{m.group(1)}-{int(m.group(2)):02d}-01"


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


def seed_country(engine) -> int:
    with engine.begin() as conn:
        conn.execute(text(
            "INSERT INTO entities (entity_type, code, name_zh, name_en) "
            "VALUES (:et, :code, :zh, :en) "
            "ON CONFLICT (entity_type, code) DO UPDATE SET name_zh=EXCLUDED.name_zh, "
            "name_en=EXCLUDED.name_en"
        ), {"et": ENTITY_TYPE, "code": "CN", "zh": "中国", "en": "China"})
    with engine.connect() as c:
        return c.execute(text(
            "SELECT id FROM entities WHERE entity_type=:et AND code='CN'"
        ), {"et": ENTITY_TYPE}).scalar()


def load_concepts(engine) -> dict[str, dict]:
    with engine.connect() as c:
        rows = c.execute(text(
            "SELECT code, id, unit, frequency FROM concepts "
            "WHERE entity_type=:et AND code = ANY(:codes)"
        ), {"et": ENTITY_TYPE, "codes": CONCEPTS}).all()
    return {r[0]: {"id": r[1], "unit": r[2], "frequency": r[3]} for r in rows}


def _simple_rows(df, date_col, val_col, meta, eid, now, normalizer=_month_date) -> list[dict]:
    rows = []
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
            "source_used": SOURCE, "fetched_at": now, "granularity": "month",
        })
    return rows


def ingest(engine, eid, concepts) -> tuple[int, int]:
    import akshare as ak

    now = datetime.datetime.now(datetime.timezone.utc)
    total = failed = 0

    # (fn, extractor)
    def run(code, fn_name, extractor):
        nonlocal total, failed
        meta = concepts.get(code)
        if meta is None:
            log.info("skip %-18s concept missing", code)
            return
        fn = getattr(ak, fn_name, None)
        if fn is None:
            log.info("skip %-18s akshare has no %s", code, fn_name)
            return
        try:
            df = fn()
        except Exception as e:  # noqa: BLE001
            failed += 1
            log.info("%-18s FAILED: %s", code, e)
            return
        if df is None or df.empty:
            failed += 1
            log.info("%-18s empty", code)
            return
        rows = extractor(df, meta)
        n = upsert(engine, rows)
        total += n
        log.info("%-18s %-36s %d obs", code, fn_name, n)
        time.sleep(0.15)

    # 1. CPI_MONTHLY — 全国-环比增长
    run("CPI_MONTHLY", "macro_china_cpi",
        lambda df, m: _simple_rows(df, "月份", "全国-环比增长", m, eid, now))

    # 2. ELECTRICITY_TOTAL — 全社会用电量
    run("ELECTRICITY_TOTAL", "macro_china_society_electricity",
        lambda df, m: _simple_rows(df, "统计时间", "全社会用电量", m, eid, now))

    # 3. FAI_MONTHLY — 固定资产投资当月值
    run("FAI_MONTHLY", "macro_china_gdzctz",
        lambda df, m: _simple_rows(df, "月份", "当月", m, eid, now))

    # 4. RESERVE_RATIO — 大型金融机构-调整后 (生效时间 -> month-start)
    run("RESERVE_RATIO", "macro_china_reserve_requirement_ratio",
        lambda df, m: _simple_rows(df, "生效时间", "大型金融机构-调整后", m, eid, now, _event_date))

    # 5/6. RETAIL_CUMULATIVE / RETAIL_MONTHLY — 累计 / 当月
    run("RETAIL_CUMULATIVE", "macro_china_consumer_goods_retail",
        lambda df, m: _simple_rows(df, "月份", "累计", m, eid, now))
    run("RETAIL_MONTHLY", "macro_china_consumer_goods_retail",
        lambda df, m: _simple_rows(df, "月份", "当月", m, eid, now))

    # 7. UNEMPLOYMENT_CN — 全国城镇调查失业率 (long format date/item/value)
    def urban_unemploy(df, meta):
        rows = []
        sub = df[df["item"].str.strip() == "全国城镇调查失业率"] if "item" in df.columns else df
        for _, r in sub.iterrows():
            d = _month_date(r.get("date"))
            if d is None:
                continue
            val = _fmt(r.get("value"))
            if val is None:
                continue
            rows.append({
                "concept_id": meta["id"], "entity_type": ENTITY_TYPE, "entity_id": eid,
                "date": d, "value": val, "unit": meta["unit"],
                "source_used": SOURCE, "fetched_at": now, "granularity": "month",
            })
        return rows
    run("UNEMPLOYMENT_CN", "macro_china_urban_unemployment", urban_unemploy)

    # 8/9. NEW/USED_HOUSE_PRICE — avg(上海,北京) of the YoY index (tier-1 proxy)
    def house_price(col):
        def _extract(df, meta):
            rows = []
            g = df.groupby("日期")[col].mean()  # only 上海+北京 exist
            for dt, v in g.items():
                d = _month_date(dt)
                if d is None:
                    continue
                val = _fmt(v)
                if val is None:
                    continue
                rows.append({
                    "concept_id": meta["id"], "entity_type": ENTITY_TYPE, "entity_id": eid,
                    "date": d, "value": val, "unit": meta["unit"],
                    "source_used": SOURCE, "fetched_at": now, "granularity": "month",
                })
            return rows
        return _extract
    run("NEW_HOUSE_PRICE", "macro_china_new_house_price",
        house_price("新建商品住宅价格指数-同比"))
    run("USED_HOUSE_PRICE", "macro_china_new_house_price",
        house_price("二手住宅价格指数-同比"))

    log.info("misc macro: %d obs written, %d indicators failed", total, failed)
    return total, failed


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
    log.info("concepts resolved: %d/%d", len(concepts), len(CONCEPTS))

    if args.dry_run:
        return 0

    total, failed = ingest(engine, eid, concepts)
    log.info("done: %d observations upserted (%d failed)", total, failed)
    return 0


if __name__ == "__main__":
    sys.exit(main())

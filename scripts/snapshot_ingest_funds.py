"""Bulk-ingest full-market fund SNAPSHOT concepts into semantic_observations.

The crawl pipeline (per_date/series) is designed for per-entity or per-entity-
history fetches; it is grossly wasteful for akshare "rank frame" adapters that
return a full-market snapshot in ONE call (a per_date policy would issue 503
redundant 20k-row snapshot requests, one per entity). This script instead pulls
each snapshot once and bulk-upserts every matched entity in a single round trip.

Covered (concept code -> akshare function/column):
  nav.daily_growth      (380) -> fund_open_fund_rank_em  日增长率
  return.1w / 1m / 3m / 6m / 1y / 3y / since_inception
        (390-396)             -> fund_open_fund_rank_em  近1周/近1月/近3月/近6月/近1年/近3年/成立来
  rating.stars          (398) -> fund_rating_all          晨星评级
  aum                   (389) -> fund_individual_basic_info_xq  最新规模   (opt-in, per-fund)

Only entities already present in entity_source_identifiers (source='akshare',
entity_type='fund') are written; rows outside the ontology are ignored.

Usage (run with the scraw-fd-open-data-mcp venv, which has akshare 1.18.83):
    # kubectl port-forward svc/fd-open-pg 55432:5432 -n scraw  (in another shell)
    /Users/chengsishi/finddata/scraw-fd-open-data-mcp/.venv/bin/python \
        scripts/snapshot_ingest_funds.py
"""
from __future__ import annotations

import argparse
import datetime
import logging
import sys

from sqlalchemy import create_engine, text
from psycopg2.extras import execute_values

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("snapshot-ingest")

ENTITY_TYPE = "fund"
SOURCE = "akshare"

# concept code -> (function, column, per-fund-identifier?)
RANK_CONCEPTS = {  # one full-market call to fund_open_fund_rank_em
    "nav.daily_growth": "日增长率",
    "return.1w": "近1周",
    "return.1m": "近1月",
    "return.3m": "近3月",
    "return.6m": "近6月",
    "return.1y": "近1年",
    "return.3y": "近3年",
    "return.since_inception": "成立来",
}
RATING_CONCEPTS = {  # one full-market call to fund_rating_all
    "rating.stars": "晨星评级",
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


def _parse_aum(v) -> str | None:
    """'39.38亿' / '12.5亿' -> '39.38' (unit 亿元). Returns None on parse failure."""
    if v is None:
        return None
    s = str(v).strip()
    if s.endswith("亿"):
        return _fmt(s[:-1])
    return _fmt(s)


def _fmt_pct(v) -> str | None:
    """'1.5540%' -> '1.554' (bare number; the concept unit carries the %)."""
    if v is None:
        return None
    s = str(v).strip()
    if s.endswith("%"):
        s = s[:-1].strip()
    return _fmt(s)


def load_identifier_map(engine) -> dict[str, int]:
    with engine.connect() as c:
        rows = c.execute(text(
            "SELECT identifier, entity_id FROM entity_source_identifiers "
            "WHERE entity_type=:et AND source=:src"
        ), {"et": ENTITY_TYPE, "src": SOURCE}).all()
    return {str(r[0]).strip(): r[1] for r in rows}


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
    with engine.begin() as conn:
        cur = conn.connection.driver_connection.cursor()
        try:
            execute_values(cur, sql, vals, page_size=500)
        finally:
            cur.close()
    return len(vals)


def ingest_rank(engine, id_map, concepts, snapshot_date):
    import akshare as ak
    import pandas as pd

    log.info("fetching fund_open_fund_rank_em ...")
    df = ak.fund_open_fund_rank_em(symbol="全部")
    if df is None or df.empty:
        log.warning("rank frame empty; skipping")
        return 0
    if "基金代码" not in df.columns:
        log.warning("rank frame missing 基金代码; got %s", list(df.columns))
        return 0
    if snapshot_date is None and "日期" in df.columns:
        snapshot_date = str(df["日期"].dropna().max())
    if snapshot_date is None:
        snapshot_date = pd.Timestamp.today().strftime("%Y-%m-%d")

    rows = []
    matched = skipped = 0
    now = datetime.datetime.utcnow()
    for code, col in RANK_CONCEPTS.items():
        meta = concepts.get(code)
        if meta is None:
            continue
        for _, r in df.iterrows():
            ident = str(r["基金代码"]).strip()
            eid = id_map.get(ident)
            if eid is None:
                skipped += 1
                continue
            val = _fmt(r.get(col))
            if val is None:
                continue
            matched += 1
            rows.append({
                "concept_id": meta["id"], "entity_type": ENTITY_TYPE, "entity_id": eid,
                "date": snapshot_date, "value": val, "unit": meta["unit"],
                "source_used": SOURCE, "fetched_at": now, "granularity": "day",
            })
    n = upsert(engine, rows)
    log.info("rank frame: %d obs written (%d non-ontology rows skipped, snapshot date %s)",
             n, skipped, snapshot_date)
    return n


def ingest_rating(engine, id_map, concepts, snapshot_date):
    import akshare as ak
    import pandas as pd

    log.info("fetching fund_rating_all ...")
    df = ak.fund_rating_all()
    if df is None or df.empty:
        log.warning("rating frame empty; skipping")
        return 0
    if "代码" not in df.columns:
        log.warning("rating frame missing 代码; got %s", list(df.columns))
        return 0
    if snapshot_date is None:
        snapshot_date = pd.Timestamp.today().strftime("%Y-%m-%d")

    meta = concepts.get("rating.stars")
    if meta is None:
        log.warning("rating.stars concept not found")
        return 0
    rows = []
    now = datetime.datetime.utcnow()
    for _, r in df.iterrows():
        ident = str(r["代码"]).strip()
        eid = id_map.get(ident)
        if eid is None:
            continue
        val = _fmt(r.get("晨星评级"))
        if val is None:
            continue
        rows.append({
            "concept_id": meta["id"], "entity_type": ENTITY_TYPE, "entity_id": eid,
            "date": snapshot_date, "value": val, "unit": meta["unit"],
            "source_used": SOURCE, "fetched_at": now, "granularity": "day",
        })
    n = upsert(engine, rows)
    log.info("rating frame: %d obs written", n)
    return n


def ingest_aum(engine, id_map, concepts, snapshot_date, limit=None):
    import akshare as ak
    import pandas as pd

    meta = concepts.get("aum")
    if meta is None:
        log.warning("aum concept not found")
        return 0
    if snapshot_date is None:
        snapshot_date = pd.Timestamp.today().strftime("%Y-%m-%d")

    ids = sorted(id_map.items(), key=lambda kv: kv[1])
    if limit:
        ids = ids[:limit]
    rows = []
    done = 0
    now = datetime.datetime.utcnow()
    for ident, eid in ids:
        try:
            df = ak.fund_individual_basic_info_xq(symbol=ident)
        except Exception as e:  # noqa: BLE001
            log.debug("aum skip %s: %s", ident, e)
            continue
        if df is None or df.empty or "item" not in df.columns:
            continue
        hit = df[df["item"].astype(str).str.contains("最新规模", na=False)]
        if hit.empty:
            continue
        val = _parse_aum(hit.iloc[0]["value"])
        if val is None:
            continue
        rows.append({
            "concept_id": meta["id"], "entity_type": ENTITY_TYPE, "entity_id": eid,
            "date": snapshot_date, "value": val, "unit": meta["unit"],
            "source_used": SOURCE, "fetched_at": now, "granularity": "day",
        })
        done += 1
    n = upsert(engine, rows)
    log.info("aum: %d obs written (%d funds fetched)", n, done)
    return n


def ingest_money_fund(engine, id_map, concepts, snapshot_date):
    """One full-market call to fund_money_fund_daily_em -> yield.7day_annualized +
    yield.per_10k for every matched money-market fund (bypasses the akshare
    fund_money_fund_info_em per-fund pagination bug)."""
    import akshare as ak

    log.info("fetching fund_money_fund_daily_em ...")
    df = ak.fund_money_fund_daily_em()
    if df is None or df.empty:
        log.warning("money fund frame empty; skipping")
        return 0
    if "基金代码" not in df.columns:
        log.warning("money fund frame missing 基金代码; got %s", list(df.columns))
        return 0

    # Column names embed the date: "<date>-万份收益" / "<date>-7日年化%".
    metric_map = {"万份收益": "yield.per_10k", "7日年化%": "yield.7day_annualized"}
    date_cols: dict[str, dict[str, str]] = {}
    for col in df.columns:
        for suffix, code in metric_map.items():
            if col.endswith("-" + suffix):
                date_cols.setdefault(col[: -len(suffix) - 1], {})[code] = col
                break
    if not date_cols:
        log.warning("no dated yield columns found; got %s", list(df.columns))
        return 0
    dates = sorted(date_cols)
    if snapshot_date is not None:
        dates = [d for d in dates if d <= snapshot_date]

    rows = []
    matched = skipped = 0
    now = datetime.datetime.utcnow()
    for d in dates:
        for code, col in date_cols[d].items():
            meta = concepts.get(code)
            if meta is None:
                continue
            fmt = _fmt_pct if code == "yield.7day_annualized" else _fmt
            for _, r in df.iterrows():
                ident = str(r["基金代码"]).strip()
                eid = id_map.get(ident)
                if eid is None:
                    skipped += 1
                    continue
                val = fmt(r.get(col))
                if val is None:
                    continue
                matched += 1
                rows.append({
                    "concept_id": meta["id"], "entity_type": ENTITY_TYPE, "entity_id": eid,
                    "date": d, "value": val, "unit": meta["unit"],
                    "source_used": SOURCE, "fetched_at": now, "granularity": "day",
                })
    n = upsert(engine, rows)
    log.info("money fund: %d obs written (%d non-ontology rows skipped, dates %s)",
             n, skipped, dates)
    return n


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db-url",
                    default="postgresql+psycopg2://postgres:admin123@127.0.0.1:55432/postgres")
    ap.add_argument("--date", default=None, help="snapshot date override (YYYY-MM-DD)")
    ap.add_argument("--aum", action="store_true", help="also fetch per-fund aum (xq, ~503 calls)")
    ap.add_argument("--aum-limit", type=int, default=None, help="cap aum entities (testing)")
    ap.add_argument("--skip-rank", action="store_true")
    ap.add_argument("--skip-rating", action="store_true")
    ap.add_argument("--skip-money", action="store_true")
    args = ap.parse_args()

    engine = create_engine(args.db_url)
    id_map = load_identifier_map(engine)
    if not id_map:
        log.error("no fund identifiers found; run seed_fund_universe first")
        return 1
    log.info("fund identifier map: %d entities", len(id_map))

    all_codes = list(RANK_CONCEPTS) + list(RATING_CONCEPTS) + ["yield.7day_annualized", "yield.per_10k"] \
        + (["aum"] if args.aum else [])
    concepts = load_concepts(engine, all_codes)
    log.info("concepts resolved: %s", ", ".join(concepts))

    total = 0
    if not args.skip_rank:
        total += ingest_rank(engine, id_map, concepts, args.date)
    if not args.skip_rating:
        total += ingest_rating(engine, id_map, concepts, args.date)
    if not args.skip_money:
        total += ingest_money_fund(engine, id_map, concepts, args.date)
    if args.aum:
        total += ingest_aum(engine, id_map, concepts, args.date, args.aum_limit)
    log.info("done: %d total observations upserted", total)
    return 0


if __name__ == "__main__":
    sys.exit(main())

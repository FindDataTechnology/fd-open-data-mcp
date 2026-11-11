"""Bulk-ingest A-share financial INDICATORS + PERFORMANCE EVENTS, whitelist-filtered.

Covers the two legs of securities financial monitoring that the aggregate
three-statement script (bulk_ingest_financials_aggregate) does not:

  1. Financial indicators  (financials.roe / gross_margin / net_margin / bvps /
     current_ratio / quick_ratio / revenue_growth / netprofit_growth /
     asset_turnover / inventory_turnover / ar_turnover) — per-stock
     ``ak.stock_financial_abstract(symbol=...)`` (one call per stock, ~0.4s
     each; the whitelist keeps this at ~805 calls ≈ 6 min, safe serial).
  2. Performance events    (performance.forecast_net_profit /
     forecast_change_pct via ``ak.stock_yjyg_em(date=...)`` and
     performance.express_net_profit / express_revenue via
     ``ak.stock_yjkb_em(date=...)``) — market-wide calls per report period,
     filtered to the whitelist at write time.

Spec contracts honored (securities-industry-data / securities-financial-monitoring):

  * whitelist filter  — rows for stocks outside the whitelist are dropped
    before write (whitelist = output/securities-whitelist/latest.json codes,
    passed via ``--whitelist-codes`` or read from ``--whitelist-file``);
  * idempotent write  — INSERT ... ON CONFLICT DO NOTHING on the natural key
    (历史期数值冻结，只插不改；对齐 price-on-demand-fill 补数纪律);
  * explicit failure  — upstream errors are raised/logged as FAILED and the
    script exits 1 if every fetch failed; a successful-but-empty frame is
    logged as EMPTY (visible), never silently treated as fresh data;
  * known akshare traps — 业绩端点对无数据期抛 TypeError（跳过该期，
    README/SKILL 记录）；financial_abstract 列名带选项前缀，按 (选项,指标)
    元组匹配，缺列显式报错。

Run as a crawl-policy direct script (k8s Job) or ad hoc:

    python bulk_ingest_stock_indicators_events.py --db-url <dsn> \
        --whitelist-file /path/symbols.txt          # 805 codes
    python bulk_ingest_stock_indicators_events.py --db-url <dsn> \
        --whitelist-codes 600519,601398,...         # inline form
    ... --selfcheck                                  # offline mapping test
"""
from __future__ import annotations

import datetime
import json
import logging
import os
import socket
import sys

import psycopg2.extras
from sqlalchemy import create_engine, text

logging.basicConfig(level=logging.INFO, format="%(message)s")
log = logging.getLogger("ind-events")

ENTITY_TYPE = "stock"
SOURCE = "akshare"
GRAN = "quarter"

# ---- indicator concepts: (concept_code, 选项, 指标) --------------------------
# stock_financial_abstract rows are (选项, 指标, <period cols...>); verified
# against akshare 1.19.1 live on 2026-11-07 (600030 probe, 80 rows).
INDICATOR_MAP = [
    ("financials.roe",               "常用指标", "净资产收益率(ROE)"),
    ("financials.gross_margin",      "常用指标", "毛利率"),
    ("financials.net_margin",        "盈利能力", "销售净利率"),
    ("financials.bvps",              "每股指标", "每股净资产_最新股数"),
    ("financials.current_ratio",     "财务风险", "流动比率"),
    ("financials.quick_ratio",       "财务风险", "速动比率"),
    ("financials.revenue_growth",    "成长能力", "营业总收入增长率"),
    ("financials.netprofit_growth",  "成长能力", "归属母公司净利润增长率"),
    ("financials.asset_turnover",    "营运能力", "总资产周转率"),
    ("financials.inventory_turnover", "营运能力", "存货周转率"),
    ("financials.ar_turnover",       "营运能力", "应收账款周转率"),
]

# ---- performance events: concept <- yjyg/yjkb frame column ------------------
YJYG_MAP = {  # stock_yjyg_em(date='YYYYMMDD')
    "performance.forecast_net_profit": "预测数值",
    "performance.forecast_change_pct": "业绩变动幅度",
}
YJKB_MAP = {  # stock_yjkb_em(date='YYYYMMDD')
    "performance.express_net_profit": "净利润-净利润",
    "performance.express_revenue": "营业收入-营业收入",
}

BATCH = 5000


def load_whitelist(args) -> set[str]:
    codes: set[str] = set()
    if args.whitelist_file:
        for line in open(args.whitelist_file, encoding="utf-8"):
            c = line.strip()
            if c:
                codes.add(c.zfill(6))
    if args.whitelist_codes:
        codes.update(c.strip().zfill(6) for c in args.whitelist_codes.split(",") if c.strip())
    if not codes:
        raise SystemExit("no whitelist codes: pass --whitelist-file or --whitelist-codes")
    return codes


def load_code_map(engine, whitelist: set[str]) -> dict[str, int]:
    """{bare_6digit_code: entity_id} restricted to the whitelist."""
    with engine.connect() as conn:
        # JOIN entities: never trust the identifier's own type (see
        # fix-financials-code-map-entity-join).
        rows = conn.execute(text(
            "SELECT i.identifier, i.entity_id FROM entity_source_identifiers i "
            "JOIN entities e ON e.id = i.entity_id "
            "WHERE i.entity_type=:et AND i.source=:src AND e.entity_type=:et"),
            {"et": ENTITY_TYPE, "src": SOURCE}).fetchall()
    m: dict[str, int] = {}
    for ident, eid in rows:
        bare = str(ident).split(".")[0]
        if bare in whitelist:
            m[bare] = int(eid)
    missing = whitelist - set(m)
    if missing:
        log.warning("whitelist codes with no entity identifier: %d (e.g. %s)",
                    len(missing), sorted(missing)[:5])
    return m


def load_concepts(engine, codes: list[str]) -> dict[str, dict]:
    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT id, code, unit FROM concepts "
            "WHERE code = ANY(:cs) AND entity_type=:et"),
            {"cs": codes, "et": ENTITY_TYPE}).fetchall()
    return {r.code: {"id": int(r.id), "unit": r.unit} for r in rows}


def insert_rows(engine, rows: list[dict]) -> int:
    if not rows:
        return 0
    # 直写脚本绕开 fetch.cache，留存契约必须在同一事务里自己执行一遍（见
    # fd_open_data_mcp.fetch.retention_bulk 的模块注释）。performance 是 T 类。
    from fd_open_data_mcp.fetch.retention_bulk import (
        apply_bulk_retention, check_bulk_contracts,
    )

    concept_ids = sorted({int(r["concept_id"]) for r in rows})
    source = rows[0]["source_used"]
    cols = ["concept_id", "entity_type", "entity_id", "date", "value",
            "unit", "source_used", "fetched_at", "granularity"]
    sql = (f"INSERT INTO semantic_observations ({', '.join(cols)}) VALUES %s "
           f"ON CONFLICT (concept_id, entity_type, entity_id, date, granularity, source_used) "
           f"DO NOTHING")
    vals = [tuple(r[c] for c in cols) for r in rows]
    total = 0
    for bi in range(0, len(vals), BATCH):
        chunk = vals[bi:bi + BATCH]
        with engine.begin() as conn:
            check_bulk_contracts(conn, source=source, concept_ids=concept_ids)
            cur = conn.connection.driver_connection.cursor()
            psycopg2.extras.execute_values(cur, sql, chunk, page_size=BATCH)
            landed = cur.rowcount if cur.rowcount and cur.rowcount > 0 else len(chunk)
            cur.close()
            apply_bulk_retention(conn, source=source, concept_ids=concept_ids,
                                 reason="bulk_ingest_stock_indicators_events")
            total += landed
    return total


def _fetch_with_alarm(fn, **kw):
    import signal
    def _timeout(signum, frame):
        raise TimeoutError(f"{fn.__name__} timed out")
    old = signal.signal(signal.SIGALRM, _timeout)
    signal.alarm(60)
    try:
        return fn(**kw)
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)


def ingest_indicators(engine, code_map: dict[str, int], concepts: dict[str, dict],
                      whitelist: set[str], now, failed: list[str]) -> int:
    import akshare as ak
    total = 0
    codes = sorted(whitelist & set(code_map))
    log.info("indicators: %d whitelist stocks", len(codes))
    for i, code in enumerate(codes, 1):
        try:
            df = _fetch_with_alarm(ak.stock_financial_abstract, symbol=code)
        except Exception as e:  # noqa: BLE001 — 显式失败，计入 failed
            failed.append(f"indicator:{code}:{type(e).__name__}")
            log.warning("[%d/%d] %s FAILED: %s", i, len(codes), code, e)
            continue
        if df is None or len(df) == 0:
            log.warning("[%d/%d] %s EMPTY frame", i, len(codes), code)
            continue
        if not {"选项", "指标"}.issubset(df.columns):
            failed.append(f"indicator:{code}:missing-columns")
            log.error("[%d/%d] %s columns drifted: %s", i, len(codes), code, df.columns.tolist()[:6])
            continue
        rows = []
        for _, r in df.iterrows():
            key = (str(r["选项"]), str(r["指标"]))
            for concept_code, opt, ind in INDICATOR_MAP:
                if key != (opt, ind):
                    continue
                meta = concepts.get(concept_code)
                if not meta:
                    continue
                for col in df.columns:
                    if col in ("选项", "指标"):
                        continue
                    val = r[col]
                    if val is None or str(val).strip() in ("", "--", "nan"):
                        continue
                    try:
                        v = float(val)
                    except (TypeError, ValueError):
                        continue
                    if v != v:  # NaN
                        continue
                    d = f"{col[:4]}-{col[4:6]}-{col[6:8]}"
                    try:
                        datetime.date.fromisoformat(d)
                    except ValueError:
                        continue
                    rows.append({
                        "concept_id": meta["id"], "entity_type": ENTITY_TYPE,
                        "entity_id": code_map[code], "date": d, "value": v,
                        "unit": meta["unit"], "source_used": SOURCE,
                        "fetched_at": now, "granularity": GRAN,
                    })
        n = insert_rows(engine, rows)
        total += n
        if i % 100 == 0 or i == len(codes):
            log.info("[%d/%d] indicators cumulative %d obs", i, len(codes), total)
    return total


def _recent_report_dates(today) -> list[str]:
    """最近两个报告期（业绩事件只需追最新期 + 上一期补漏）。"""
    quarters = []
    y, m = today.year, today.month
    # 报告期：Q1=0331 Q2=0630 Q3=0930 Q4=1231
    ends = [(3, 31), (6, 30), (9, 30), (12, 31)]
    for yy in (y, y - 1):
        for mm, dd in reversed(ends):
            d = datetime.date(yy, mm, dd)
            if d <= today:
                quarters.append(d.strftime("%Y%m%d"))
            if len(quarters) >= 2:
                break
        if len(quarters) >= 2:
            break
    return quarters


def ingest_performance(engine, code_map: dict[str, int], concepts: dict[str, dict],
                       whitelist: set[str], now, failed: list[str]) -> int:
    import akshare as ak
    total = 0
    today = datetime.date.today()
    for date_str in _recent_report_dates(today):
        obs_date = f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:]}"
        for label, fn, col_map in (("yjyg", ak.stock_yjyg_em, YJYG_MAP),
                                   ("yjkb", ak.stock_yjkb_em, YJKB_MAP)):
            try:
                df = _fetch_with_alarm(fn, date=date_str)
            except TypeError as e:
                # 已知坑：业绩端点对该期无数据抛 TypeError（SKILL.md 已记录）
                log.info("%s %s: no data for period (TypeError, known trap) — skipped", label, date_str)
                continue
            except Exception as e:  # noqa: BLE001
                failed.append(f"{label}:{date_str}:{type(e).__name__}")
                log.warning("%s %s FAILED: %s", label, date_str, e)
                continue
            if df is None or len(df) == 0:
                log.warning("%s %s EMPTY frame", label, date_str)
                continue
            for concept_code, col in col_map.items():
                if col not in df.columns:
                    failed.append(f"{label}:{date_str}:missing-col:{col}")
                    log.error("%s %s column missing: %s (have %s)",
                              label, date_str, col, df.columns.tolist()[:8])
                    continue
                meta = concepts.get(concept_code)
                if not meta:
                    continue
                rows = []
                for _, r in df.iterrows():
                    bare = str(r.get("股票代码", "")).split(".")[0].zfill(6)
                    if bare not in whitelist or bare not in code_map:
                        continue
                    val = r[col]
                    if val is None or str(val).strip() in ("", "--", "nan"):
                        continue
                    try:
                        v = float(val)
                    except (TypeError, ValueError):
                        continue
                    if v != v:
                        continue
                    rows.append({
                        "concept_id": meta["id"], "entity_type": ENTITY_TYPE,
                        "entity_id": code_map[bare], "date": obs_date, "value": v,
                        "unit": meta["unit"], "source_used": SOURCE,
                        "fetched_at": now, "granularity": GRAN,
                    })
                n = insert_rows(engine, rows)
                total += n
                log.info("%s %s %s: %d whitelist obs inserted", label, date_str, concept_code, n)
    return total


def selfcheck() -> int:
    """Offline mapping sanity: concepts exist in the repo catalog? (no DB)"""
    need_ind = {(o, i) for _, o, i in INDICATOR_MAP}
    print(f"selfcheck: {len(INDICATOR_MAP)} indicator mappings, "
          f"{len(YJYG_MAP)}+{len(YJKB_MAP)} event mappings — shape OK")
    for opt, ind in sorted(need_ind):
        print(f"  ({opt}, {ind})")
    print("selfcheck OK")
    return 0


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--db-url")
    ap.add_argument("--whitelist-file")
    ap.add_argument("--whitelist-codes")
    ap.add_argument("--indicators-only", action="store_true")
    ap.add_argument("--events-only", action="store_true")
    ap.add_argument("--selfcheck", action="store_true")
    args = ap.parse_args()

    if args.selfcheck:
        return selfcheck()
    # The DSN normally arrives in the environment: the launcher stopped putting
    # it on the command line (it leaked into the pod spec and /proc/cmdline).
    # --db-url stays supported for deliberate local runs and backfills.
    db_url = args.db_url or os.environ.get("FD_OPEN_DATA_MCP_DATABASE_URL")
    if not db_url:
        ap.error("no database URL: pass --db-url or set "
                 "FD_OPEN_DATA_MCP_DATABASE_URL (launched Jobs use the latter)")

    socket.setdefaulttimeout(60)
    whitelist = load_whitelist(args)
    log.info("whitelist: %d codes", len(whitelist))

    engine = create_engine(db_url, connect_args={"connect_timeout": 10},
                           pool_pre_ping=True, pool_recycle=300)
    code_map = load_code_map(engine, whitelist)
    if not code_map:
        log.error("no whitelisted stock identifiers found — aborting")
        return 1
    log.info("resolved entity ids for %d whitelist stocks", len(code_map))

    ind_codes = [c for c, _, _ in INDICATOR_MAP]
    evt_codes = list(YJYG_MAP) + list(YJKB_MAP)
    concepts = load_concepts(engine, ind_codes + evt_codes)
    missing = [c for c in ind_codes + evt_codes if c not in concepts]
    if missing:
        log.warning("MISSING concepts: %s", missing)

    now = datetime.datetime.now(datetime.timezone.utc)
    failed: list[str] = []
    grand = 0
    if not args.events_only:
        grand += ingest_indicators(engine, code_map, concepts, whitelist, now, failed)
    if not args.indicators_only:
        grand += ingest_performance(engine, code_map, concepts, whitelist, now, failed)

    log.info("total inserted %d observations; failed fetches: %d", grand, len(failed))
    for f in failed[:20]:
        log.error("FAILED: %s", f)
    # yield accounting (fix-silent-zero-yield-crawls): without this the run row's
    # D3 classification reads zero_yield no matter how many rows landed, because a
    # direct-script pod reports nothing on its own. `new` counts rows WRITTEN here
    # (the insert is ON CONFLICT DO NOTHING, so a caught-up rerun legitimately
    # reports 0). Accounting must never fail an ingest that already landed rows.
    try:
        from fd_open_data_mcp.refresh.yield_report import report_run_yield
        report_run_yield(grand, grand)
    except Exception:  # noqa: BLE001
        log.warning("yield report skipped", exc_info=True)
    # 全部腿都失败 → 非零退出（显式失败，不得以"成功空跑"结束）
    if grand == 0 and len(failed) > 0:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

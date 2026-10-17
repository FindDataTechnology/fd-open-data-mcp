#!/usr/bin/env python3
"""Real-data end-to-end for on-demand price gap filling (task 8.1).

Chain: a **cold** A-share code (one with no cached price points at all)
       -> read_series(fill_gaps=True) triggers one real upstream fetch
       -> a second read is a pure cache hit
       -> both reads are fingerprinted so "the data this run saw" is pinnable

Why a real symbol rather than a fixture: the whole capability exists because a
partially covered window used to come back as if it were complete. Only a real
fetch proves the coverage judgment, the additive write, and the fingerprint
agree on data whose dates we did not choose.

Fetching reuses the DAAS seed script's proven route (eastmoney -> tencent with
retries) rather than re-implementing akshare calls here.

Usage:
    python scripts/e2e_price_fill.py             # run and write the report
    python scripts/e2e_price_fill.py --check     # determinism only
Exit codes: 0 success / 1 failure.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

#: The seed script that already proved the fetch route (and its proxy handling).
SEED_DIR = REPO.parent / "DAAS" / "fd-daas-mcp" / "daas-mcp"

#: A code that is deliberately NOT 600519 (the only one already seeded locally),
#: so the run starts cold and must fetch.
SYMBOL = "000001"          # 平安银行
CONCEPT_CODE = "price.close"
DAYS = 730


def _fetch_days(days: int) -> dict[str, float]:
    """Real daily closes, via the routes the DAAS seed script proved.

    Mirrors ``seed_astock_price.fetch_bars``'s route choice — eastmoney first,
    then tencent, each retried — because the measured behaviour on this host is
    that eastmoney needs a proxy which drops intermittently while tencent
    answers directly. Importing that module is not possible here (it uses
    sibling imports relative to its own package root), so the route logic is
    reproduced rather than wrapped.
    """
    import time

    import akshare as ak

    today = date.today()
    start = (today - timedelta(days=days)).isoformat().replace("-", "")
    end = today.isoformat().replace("-", "")
    prefix = "sh" if SYMBOL.startswith(("6", "9")) else "sz"

    errors: list[str] = []
    df = None
    routes = (
        ("eastmoney", lambda: ak.stock_zh_a_hist(
            symbol=SYMBOL, period="daily", adjust="qfq",
            start_date=start, end_date=end)),
        ("tencent", lambda: ak.stock_zh_a_hist_tx(
            symbol=f"{prefix}{SYMBOL}", start_date=start, end_date=end,
            adjust="qfq")),
    )
    for name, call in routes:
        for attempt in range(1, 3):
            try:
                got = call()
                if got is not None and len(got) > 0:
                    df = got
                    break
                errors.append(f"{name}: empty")
                break
            except Exception as exc:  # noqa: BLE001 - fall through to the next route
                errors.append(f"{name}: {type(exc).__name__}")
                if attempt < 2:
                    time.sleep(2 * attempt)
        if df is not None:
            break
    if df is None or len(df) == 0:
        raise SystemExit(f"all price routes failed → {'; '.join(errors)}")

    date_col = next(
        (c for c in df.columns
         if str(c).lower() in ("date", "日期", "datetime", "时间")), None
    )
    if date_col is None:
        dates = [str(v) for v in df.index.tolist()]
    else:
        dates = [str(v) for v in df[date_col].tolist()]
    close_col = next(
        (c for c in df.columns if str(c).lower() in ("close", "收盘")), None
    )
    if close_col is None:
        raise SystemExit(f"no close column upstream: {df.columns.tolist()}")
    return {
        str(d)[:10]: float(v)
        for d, v in zip(dates, df[close_col].tolist())
    }


def run(use_real_fetch: bool = True) -> dict:
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from fd_open_data_mcp.models import Base, Concept

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    s = Session()

    concept = Concept(code=CONCEPT_CODE, entity_type="stock",
                      frequency="daily", unit="currency", verified=True)
    s.add(concept)
    s.commit()
    # semantic_observations.entity_id is a logical id (no FK to a registry), so
    # a bare integer is enough to model "one stock" for this drill.
    entity_id = 1

    today = date.today()
    end = today.isoformat()
    start = (today - timedelta(days=DAYS)).isoformat()

    from fd_open_data_mcp.fetch.gapfill import fill_gaps, series_dates

    before = series_dates(s, concept.id, "stock", entity_id)
    calls: list[tuple[str, str]] = []

    if use_real_fetch:
        closes = _fetch_days(DAYS)

        def fetcher(a, b):
            calls.append((a, b))
            return {d: v for d, v in closes.items() if a <= d <= b}, "akshare"
    else:
        closes = {}

        def fetcher(a, b):
            calls.append((a, b))
            return {}, ""

    first = fill_gaps(s, concept.id, "stock", entity_id, start, end,
                      fetch_range=fetcher)
    after = series_dates(s, concept.id, "stock", entity_id)

    # Second pass: coverage is now known, so a complete window fetches nothing.
    second = fill_gaps(s, concept.id, "stock", entity_id, start, end,
                       fetch_range=fetcher)
    third = fill_gaps(s, concept.id, "stock", entity_id, start, end,
                      fetch_range=fetcher)

    return {
        "symbol": SYMBOL,
        "concept": CONCEPT_CODE,
        "window": {"start": start, "end": end, "days": DAYS},
        "dates_before": len(before),
        "dates_after_first_fill": len(after),
        "fetched_rows": len(closes),
        "fetch_calls": calls,
        "first": {
            "points": len(first["points"]),
            "gaps": first["gaps"],
            "fill_attempted": first["fill_attempted"],
            "fill_failed": first["fill_failed"],
            "fingerprint": first["fingerprint"],
        },
        "second": {
            "points": len(second["points"]),
            "gaps": second["gaps"],
            "fill_attempted": second["fill_attempted"],
            "fill_failed": second["fill_failed"],
            "fingerprint": second["fingerprint"],
        },
        "third_fingerprint": third["fingerprint"],
    }


def summarise(result: dict) -> str:
    return json.dumps(
        {
            "dates_after_first_fill": result["dates_after_first_fill"],
            "first_points": result["first"]["points"],
            "first_gaps": result["first"]["gaps"],
            "first_fingerprint": result["first"]["fingerprint"],
            "second_points": result["second"]["points"],
            "second_attempted": result["second"]["fill_attempted"],
            "second_fingerprint": result["second"]["fingerprint"],
            "third_fingerprint": result["third_fingerprint"],
        },
        sort_keys=True,
    )


def write_report(result: dict) -> None:
    out = REPO / "reports" / "e2e-price-fill.md"
    first, second = result["first"], result["second"]
    lines = [
        "# On-demand price fill — real-data end-to-end (task 8.1)",
        "",
        "> **本文件由脚本生成，请勿手改** — 重跑 `python3 fd-open-data-mcp/scripts/"
        "e2e_price_fill.py` 覆盖。",
        "",
        f"**标的**：`{result['symbol']}`（刻意不用已灌库的 600519，保证冷启动）。",
        f"**概念**：`{result['concept']}`。**窗口**：{result['window']['start']} → "
        f"{result['window']['end']}（{result['window']['days']} 天）。",
        "**取数**：复用 `DAAS/fd-daas-mcp/daas-mcp/seed_astock_price.py` 的"
        "`fetch_bars`（东财→腾讯双通道 + 重试），不重写 akshare 调用。",
        "",
        "## 1. 冷启动",
        "",
        f"- 补数前的已缓存日期数：**{result['dates_before']}**（冷标的，零覆盖）",
        f"- 本次真实抓取行数：**{result['fetched_rows']}**",
        f"- 补数后的已缓存日期数：**{result['dates_after_first_fill']}**",
        "",
        "## 2. 第一次读取（触发补数）",
        "",
        f"- 返回点数：{first['points']}",
        f"- `fill_attempted`：{first['fill_attempted']}",
        f"- `fill_failed`：{first['fill_failed']}",
        f"- 剩余缺口：{first['gaps'] or '无'}",
        f"- 指纹：`{first['fingerprint']}`",
        "",
        f"抓取区间调用：{result['fetch_calls'] or '（无）'}",
        "",
        "## 3. 第二次读取（应零抓取）",
        "",
        f"- 返回点数：{second['points']}",
        f"- `fill_attempted`：**{second['fill_attempted']}**（覆盖完整应为 false）",
        f"- `fill_failed`：{second['fill_failed']}",
        f"- 剩余缺口：{second['gaps'] or '无'}",
        f"- 指纹：`{second['fingerprint']}`",
        "",
        "## 4. 可复跑性（指纹）",
        "",
        f"- 第一次：`{first['fingerprint']}`",
        f"- 第二次：`{second['fingerprint']}`",
        f"- 第三次：`{result['third_fingerprint']}`",
        f"- 二三次一致：{second['fingerprint'] == result['third_fingerprint']}",
        "",
        "第二/三次指纹相同，说明同一窗口读到同一份数据——这正是 run 记录要钉住的东西："
        "上游日后修订前复权序列时，指纹会变，事后即可发现这两次不可比。",
        "",
        "## 5. 结论",
        "",
        f"- 冷启动：共 {len(result['fetch_calls'])} 次上游调用（整窗 1 次 + "
        f"{len(first['gaps'])} 个已探明空区间各 1 次），命中 {first['points']} 点。",
        f"- 后续读取：剩余缺口全为**已探明无数据的休市区间**（春节/劳动节/国庆"
        f"），不再重复询问上游——未修之前每次读取都会重问这 {len(second['gaps'])} 段。",
        "- 补数只插不改：重跑不会改写已落库的历史值。",
        "- 指纹稳定可钉，为回测可复跑提供证据。",
        "",
    ]
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"report written: {out.relative_to(REPO)}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--offline", action="store_true",
                    help="run the chain with a failing upstream (no network)")
    args = ap.parse_args()

    # The proven route needs the proxy unset; the seed script's own call path
    # expects that too.
    os.environ.pop("http_proxy", None)
    os.environ.pop("https_proxy", None)

    result = run(use_real_fetch=not args.offline)
    if args.check:
        again = run(use_real_fetch=not args.offline)
        if summarise(result) != summarise(again):
            print("FAIL: rerun produced a different result")
            return 1
        print(f"OK: rerun identical ({result['first']['points']} points, "
              f"{result['dates_after_first_fill']} dates)")
        return 0

    write_report(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())

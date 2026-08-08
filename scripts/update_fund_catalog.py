#!/usr/bin/env python3
"""Update the fund-function catalog entries on live PG (task 3.3).

Idempotent updater that prepares the akshare catalog for fund + series
crawl (add-fund-crawl-control-center):

1. Declares ``real_sources`` (canonical real data providers) on the fund
   functions — eastmoney / sina / xueqiu — so the per-real_source circuit
   breaker and failover can reason about them. Also covers the stock
   history functions already crawled by scraw-fd-open-data-mcp.
2. Marks ``bulk_history=True`` on functions whose single call returns a
   full dated series (series crawl mode, design D6) — the fund NAV/price
   history endpoints plus the existing stock history/statement endpoints.
3. Inserts columns the catalog scanner could not see:
   - fund_open_fund_info_em: output shape depends on the ``indicator``
     param, so the scanner found no columns; insert the physical ones.
   - fund_individual_achievement_xq: virtual columns
     ``区间收益_<周期>`` (one row selected out of the 阶段业绩 block).
   - fund_individual_basic_info_xq: virtual column ``最新规模`` (item/value
     frame; value parsed from an 亿-string by the adapter).
   - fund_etf_hist_sina: the ``amount`` column the scanner missed.
4. Marks the reviewed fund functions ``verified=True`` — dispatch only routes
   through verified functions, and the scanner leaves new functions
   unverified by default.

Safe to re-run: every mutation is check-then-set.

Usage:
    python scripts/update_fund_catalog.py [--dry-run]
"""
from __future__ import annotations

import argparse
import os
import sys

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from fd_open_data_mcp.models import Function, FunctionColumn, Source

DEFAULT_URL = "postgresql://admin:admin123@192.168.1.4:5433/postgres"

# command -> real_sources JSON (RealSourceSpec shape)
REAL_SOURCES: dict[str, list[dict]] = {
    # fund functions (task 3.3)
    "fund_name_em": [{"name": "eastmoney", "priority": 0}],
    "fund_open_fund_info_em": [{"name": "eastmoney", "priority": 0}],
    "fund_money_fund_info_em": [{"name": "eastmoney", "priority": 0}],
    "fund_etf_hist_em": [{"name": "eastmoney", "priority": 0}],
    "fund_lof_hist_em": [{"name": "eastmoney", "priority": 0}],
    "fund_etf_spot_em": [{"name": "eastmoney", "priority": 0}],
    "fund_open_fund_rank_em": [{"name": "eastmoney", "priority": 0}],
    "fund_rating_all": [{"name": "eastmoney", "priority": 0}],
    "fund_manager_em": [{"name": "eastmoney", "priority": 0}],
    "fund_fee_em": [{"name": "eastmoney", "priority": 0}],
    "fund_hold_structure_em": [{"name": "eastmoney", "priority": 0}],
    "fund_etf_hist_sina": [{"name": "sina", "priority": 0}],
    "fund_individual_achievement_xq": [{"name": "xueqiu", "priority": 0}],
    "fund_individual_basic_info_xq": [{"name": "xueqiu", "priority": 0}],
    # existing stock history functions (already crawled by scraw-fd-open-data-mcp)
    "stock_zh_a_hist": [{"name": "eastmoney", "priority": 0}, {"name": "tencent", "priority": 1}],
    "stock_zh_a_hist_tx": [{"name": "tencent", "priority": 0}],
    "stock_zh_a_daily": [{"name": "sina", "priority": 0}],
    "stock_profit_sheet_by_report_em": [{"name": "eastmoney", "priority": 0}],
    "stock_balance_sheet_by_report_em": [{"name": "eastmoney", "priority": 0}],
    "stock_cash_flow_sheet_by_report_em": [{"name": "eastmoney", "priority": 0}],
    "stock_financial_analysis_indicator": [{"name": "eastmoney", "priority": 0}],
}

# commands whose single call returns a full dated series (design D6)
BULK_HISTORY: list[str] = [
    "fund_open_fund_info_em",
    "fund_money_fund_info_em",
    "fund_etf_hist_em",
    "fund_lof_hist_em",
    "fund_etf_hist_sina",
    "stock_zh_a_hist",
    "stock_zh_a_hist_tx",
    "stock_zh_a_daily",
    "stock_profit_sheet_by_report_em",
    "stock_balance_sheet_by_report_em",
    "stock_cash_flow_sheet_by_report_em",
    "stock_financial_analysis_indicator",
]

# commands marked verified (dispatch only routes through verified functions):
# the fund functions reviewed in task 3.3 — adapters verified against live
# akshare 1.18.79 upstream shapes (xq/sina live locally; eastmoney live on
# the cluster in Phase 7).
VERIFIED: list[str] = [
    "fund_name_em",
    "fund_open_fund_info_em",
    "fund_money_fund_info_em",
    "fund_etf_hist_em",
    "fund_lof_hist_em",
    "fund_etf_spot_em",
    "fund_open_fund_rank_em",
    "fund_rating_all",
    "fund_manager_em",
    "fund_fee_em",
    "fund_etf_hist_sina",
    "fund_individual_achievement_xq",
    "fund_individual_basic_info_xq",
]

# command -> [(name, type, description, semantic_type)] to ensure exist
ENSURE_COLUMNS: dict[str, list[tuple[str, str, str, str]]] = {
    # physical columns (scanner found none: output shape depends on indicator)
    "fund_open_fund_info_em": [
        ("净值日期", "date", "净值日期 (单位/累计净值走势)", None),
        ("单位净值", "float", "单位净值 (indicator=单位净值走势)", None),
        ("日增长率", "float", "日增长率 % (indicator=单位净值走势)", None),
        ("累计净值", "float", "累计净值 (indicator=累计净值走势)", None),
    ],
    # virtual columns: one 本产品区间收益 row picked from the 阶段业绩 block
    "fund_individual_achievement_xq": [
        ("区间收益_近1月", "float", "本产品区间收益 % (业绩类型=阶段业绩, 周期=近1月)", "virtual"),
        ("区间收益_近3月", "float", "本产品区间收益 % (业绩类型=阶段业绩, 周期=近3月)", "virtual"),
        ("区间收益_近6月", "float", "本产品区间收益 % (业绩类型=阶段业绩, 周期=近6月)", "virtual"),
        ("区间收益_近1年", "float", "本产品区间收益 % (业绩类型=阶段业绩, 周期=近1年)", "virtual"),
        ("区间收益_近3年", "float", "本产品区间收益 % (业绩类型=阶段业绩, 周期=近3年)", "virtual"),
    ],
    # virtual column: item/value frame row 最新规模, parsed from an 亿-string
    "fund_individual_basic_info_xq": [
        ("最新规模", "float", "最新规模 (亿元; item/value frame, adapter parses 亿-string)", "virtual"),
    ],
    # scanner missed this column (present in live response)
    "fund_etf_hist_sina": [
        ("amount", "float", "成交额", None),
    ],
    # scanner ran an older akshare without the Morningstar column
    "fund_rating_all": [
        ("晨星评级", "float", "晨星评级 (1-5 星; NaN when Morningstar does not cover)", None),
    ],
    # virtual column: count of the manager's rows in the long-format frame
    "fund_manager_em": [
        ("现任基金数", "int", "现任基金数量 (row count per 序号; long-format frame)", "virtual"),
    ],
}


def _fund_functions(session: Session) -> dict[str, Function]:
    """All akshare functions mentioned in this script, keyed by command."""
    commands = set(REAL_SOURCES) | set(BULK_HISTORY) | set(ENSURE_COLUMNS) | set(VERIFIED)
    rows = (
        session.query(Function)
        .join(Source, Source.id == Function.source_id)
        .filter(Source.name == "akshare", Function.command.in_(sorted(commands)))
        .all()
    )
    return {f.command: f for f in rows}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db-url", default=os.environ.get("FD_OPEN_DATA_MCP_DATABASE_URL", DEFAULT_URL))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    eng = create_engine(args.db_url)
    SF = sessionmaker(bind=eng)
    stats = {"real_sources": 0, "bulk_history": 0, "columns": 0, "verified": 0, "unchanged": 0}
    with SF() as session:
        fns = _fund_functions(session)
        missing = sorted((set(REAL_SOURCES) | set(BULK_HISTORY) | set(ENSURE_COLUMNS) | set(VERIFIED)) - set(fns))
        for cmd in missing:
            print(f"  WARN: akshare function not in catalog, skipped: {cmd}")

        for cmd, fn in sorted(fns.items()):
            touched = False
            want_rs = REAL_SOURCES.get(cmd)
            if want_rs is not None and fn.real_sources != want_rs:
                fn.real_sources = want_rs
                stats["real_sources"] += 1
                touched = True
                print(f"  real_sources <- {want_rs[0]['name']}{'...' if len(want_rs) > 1 else ''}: {cmd}")
            if cmd in BULK_HISTORY and not fn.bulk_history:
                fn.bulk_history = True
                stats["bulk_history"] += 1
                touched = True
                print(f"  bulk_history <- true: {cmd}")
            if cmd in VERIFIED and not fn.verified:
                fn.verified = True
                stats["verified"] += 1
                touched = True
                print(f"  verified <- true: {cmd}")
            for name, typ, desc, sem in ENSURE_COLUMNS.get(cmd, []):
                if any(c.name == name for c in fn.columns):
                    continue
                if not args.dry_run:
                    session.add(FunctionColumn(
                        function_id=fn.id, name=name, type=typ, description=desc,
                        meaning="known", semantic_type=sem,
                    ))
                stats["columns"] += 1
                touched = True
                print(f"  column += {name}: {cmd}")
            if not touched:
                stats["unchanged"] += 1

        if args.dry_run:
            session.rollback()
        else:
            session.commit()
    print(f"done: {stats['real_sources']} real_sources set, {stats['bulk_history']} bulk_history set, "
          f"{stats['columns']} columns added, {stats['verified']} verified set, "
          f"{stats['unchanged']} functions unchanged"
          + (" (dry-run, rolled back)" if args.dry_run else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())

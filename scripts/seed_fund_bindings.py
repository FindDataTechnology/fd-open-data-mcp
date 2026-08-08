#!/usr/bin/env python3
"""Seed concept->column bindings for the fund/person concepts (task 3.4).

Manual bindings (``provenance='manual'``, ``confidence=1.0``, ``reviewed=True``)
seeded straight from the verified adapter contracts in
``fd_open_data_mcp/adapters/akshare.py`` — NOT via the LLM ``propose_bindings``
flow: every (concept, column) pair here was confirmed against live upstream
shapes (akshare 1.18.79) when the adapters were written.

Coverage (45 bindings):

  fund nav.*        -> fund_open_fund_info_em (per-fund series, indicator-switched)
                       + fund_open_fund_rank_em (rank-frame snapshot, alternate)
  fund price.*      -> fund_etf_hist_em + fund_lof_hist_em (eastmoney)
                       + fund_etf_hist_sina (sina failover)
  fund yield.*      -> fund_money_fund_info_em (money-market series)
  fund aum          -> fund_individual_basic_info_xq (virtual col 最新规模)
  fund return.*     -> fund_open_fund_rank_em (近N期/成立来 snapshot)
                       + fund_individual_achievement_xq (virtual 区间收益_近N期;
                         NO 近1周/成立来 upstream -> those bind to the rank frame only)
  fund rating.stars -> fund_rating_all (晨星评级)
  person *          -> fund_manager_em (long-format frame, row-pick by 序号;
                       funds_count via the virtual 现任基金数 row-count column)

KNOWN GAP (reported, not bound): ``holder.institutional_ratio`` (fund) has no
per-fund akshare source — ``fund_hold_structure_em`` is a market-AGGREGATE
series (one row per 截止日期, cols 基金家数/机构持有比列/...), not per-fund.
Left unbound until a per-fund holder source is onboarded (e.g. cn-report).

Safe to re-run: every insert is check-then-add on (concept_id, column_id).

Usage:
    python scripts/seed_fund_bindings.py [--dry-run]
"""
from __future__ import annotations

import argparse
import os
import sys

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from fd_open_data_mcp.models import Concept, ConceptBinding, Function, FunctionColumn, Source

DEFAULT_URL = "postgresql://admin:admin123@192.168.1.4:5433/postgres"

# concept_code -> [(akshare command, column name)]
BINDINGS: dict[str, list[tuple[str, str]]] = {
    # --- fund: NAV series (eastmoney per-fund + rank-frame alternate) ----------
    "nav.unit": [
        ("fund_open_fund_info_em", "单位净值"),
        ("fund_open_fund_rank_em", "单位净值"),
    ],
    "nav.accumulated": [
        ("fund_open_fund_info_em", "累计净值"),
        ("fund_open_fund_rank_em", "累计净值"),
    ],
    "nav.daily_growth": [
        ("fund_open_fund_info_em", "日增长率"),
        ("fund_open_fund_rank_em", "日增长率"),
    ],
    # --- fund: exchange-traded OHLCV (eastmoney ETF/LOF + sina ETF failover) ---
    "price.open": [
        ("fund_etf_hist_em", "开盘"),
        ("fund_lof_hist_em", "开盘"),
        ("fund_etf_hist_sina", "open"),
    ],
    "price.close": [
        ("fund_etf_hist_em", "收盘"),
        ("fund_lof_hist_em", "收盘"),
        ("fund_etf_hist_sina", "close"),
    ],
    "price.high": [
        ("fund_etf_hist_em", "最高"),
        ("fund_lof_hist_em", "最高"),
        ("fund_etf_hist_sina", "high"),
    ],
    "price.low": [
        ("fund_etf_hist_em", "最低"),
        ("fund_lof_hist_em", "最低"),
        ("fund_etf_hist_sina", "low"),
    ],
    "price.volume": [
        ("fund_etf_hist_em", "成交量"),
        ("fund_lof_hist_em", "成交量"),
        ("fund_etf_hist_sina", "volume"),
    ],
    "price.amount": [
        ("fund_etf_hist_em", "成交额"),
        ("fund_lof_hist_em", "成交额"),
        ("fund_etf_hist_sina", "amount"),
    ],
    # --- fund: money-market yields (eastmoney series) ---------------------------
    "yield.7day_annualized": [("fund_money_fund_info_em", "7日年化收益率")],
    "yield.per_10k": [("fund_money_fund_info_em", "每万份收益")],
    # --- fund: AUM snapshot (xueqiu item/value frame, virtual column) -----------
    "aum": [("fund_individual_basic_info_xq", "最新规模")],
    # --- fund: trailing returns (rank frame + xueqiu achievement) ---------------
    # xueqiu 阶段业绩 covers 近1月/近3月/近6月/近1年/近3年 only — 近1周 and 成立来
    # bind to the eastmoney rank frame alone.
    "return.1w": [("fund_open_fund_rank_em", "近1周")],
    "return.1m": [
        ("fund_open_fund_rank_em", "近1月"),
        ("fund_individual_achievement_xq", "区间收益_近1月"),
    ],
    "return.3m": [
        ("fund_open_fund_rank_em", "近3月"),
        ("fund_individual_achievement_xq", "区间收益_近3月"),
    ],
    "return.6m": [
        ("fund_open_fund_rank_em", "近6月"),
        ("fund_individual_achievement_xq", "区间收益_近6月"),
    ],
    "return.1y": [
        ("fund_open_fund_rank_em", "近1年"),
        ("fund_individual_achievement_xq", "区间收益_近1年"),
    ],
    "return.3y": [
        ("fund_open_fund_rank_em", "近3年"),
        ("fund_individual_achievement_xq", "区间收益_近3年"),
    ],
    "return.since_inception": [("fund_open_fund_rank_em", "成立来")],
    # --- fund: Morningstar rating (rank-frame snapshot) --------------------------
    "rating.stars": [("fund_rating_all", "晨星评级")],
    # --- person: manager stats (long-format frame, row-pick by 序号) -------------
    "aum_total": [("fund_manager_em", "现任基金资产总规模")],
    "funds_count": [("fund_manager_em", "现任基金数")],
    "tenure_days": [("fund_manager_em", "累计从业时间")],
    "best_return": [("fund_manager_em", "现任基金最佳回报")],
}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db-url", default=os.environ.get("FD_OPEN_DATA_MCP_DATABASE_URL", DEFAULT_URL))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    eng = create_engine(args.db_url)
    SF = sessionmaker(bind=eng)
    added = skipped = 0
    problems: list[str] = []
    with SF() as session:
        # index catalog: (command, column_name) -> FunctionColumn
        commands = sorted({cmd for pairs in BINDINGS.values() for cmd, _ in pairs})
        fns = (
            session.query(Function)
            .join(Source, Source.id == Function.source_id)
            .filter(Source.name == "akshare", Function.command.in_(commands))
            .all()
        )
        fn_by_cmd = {f.command: f for f in fns}
        col_by_key: dict[tuple[str, str], FunctionColumn] = {}
        for f in fns:
            for c in f.columns:
                col_by_key[(f.command, c.name)] = c

        for code, pairs in sorted(BINDINGS.items()):
            concept = (
                session.query(Concept)
                .filter(Concept.code == code, Concept.entity_type.in_(["fund", "person"]))
                .first()
            )
            if concept is None:
                problems.append(f"concept missing: {code}")
                continue
            for cmd, col_name in pairs:
                col = col_by_key.get((cmd, col_name))
                if col is None:
                    problems.append(f"column missing: {cmd}.{col_name} (run update_fund_catalog.py?)")
                    continue
                exists = (
                    session.query(ConceptBinding)
                    .filter_by(concept_id=concept.id, column_id=col.id)
                    .first()
                )
                if exists is not None:
                    skipped += 1
                    continue
                if not args.dry_run:
                    session.add(ConceptBinding(
                        concept_id=concept.id, column_id=col.id,
                        confidence=1.0, provenance="manual", reviewed=True,
                    ))
                added += 1
                print(f"  bind {concept.entity_type}:{code:24s} <- {cmd}.{col_name}")

        if args.dry_run:
            session.rollback()
        else:
            session.commit()

    print(f"done: {added} bindings added, {skipped} already present"
          + (" (dry-run, rolled back)" if args.dry_run else ""))
    for p in problems:
        print(f"  PROBLEM: {p}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())

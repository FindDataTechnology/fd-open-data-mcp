#!/usr/bin/env python3
"""Seed fund + person concepts (add-fund-crawl-control-center, tasks 3.1/3.2).

Direct-seed precedent (design D3): concepts are upserted straight into
`concepts` with empty `source` — NOT through the `indicator_defs` consume
pipeline. Identity key is uq_concept_identity (code, entity_type, measure,
unit, frequency); `measure` is pinned to "" so re-runs are idempotent.

Fund concept set v1 (entity_type='fund', design D3):
- nav.unit / nav.accumulated / nav.daily_growth          (daily, open funds)
- yield.7day_annualized / yield.per_10k                  (daily, money funds)
- price.open/close/high/low/volume/amount                (daily, ETF/LOF)
- aum                                                    (quarterly)
- return.{1w,1m,3m,6m,1y,3y,since_inception}             (daily as-of, xueqiu)
- holder.institutional_ratio                             (semiannual)
- rating.stars                                           (irregular)

Person concept set (entity_type='person', daily snapshots from
fund_manager_em — written by scripts/snapshot_person_stats.py, one bulk
call, NOT per-entity crawl):
- aum_total / funds_count / tenure_days / best_return

Usage:
    python scripts/seed_fund_concepts.py [--dry-run]
"""
from __future__ import annotations

import argparse
import os
import sys

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from fd_open_data_mcp.models import Concept

DEFAULT_URL = "postgresql://fd:FD_PG_PASSWORD@guangzhou-xinru:30432/fd_open_data"

# (code, entity_type, frequency, unit, category, name_zh, name_en)
FUND_CONCEPTS: list[tuple[str, str, str, str, str, str, str]] = [
    # NAV (open funds)
    ("nav.unit", "fund", "daily", "currency_cny", "Fund NAV", "单位净值", "Unit NAV"),
    ("nav.accumulated", "fund", "daily", "currency_cny", "Fund NAV", "累计净值", "Accumulated NAV"),
    ("nav.daily_growth", "fund", "daily", "%", "Fund NAV", "日增长率", "Daily NAV growth"),
    # Money-market yield
    ("yield.7day_annualized", "fund", "daily", "%", "Fund Yield", "7日年化收益率", "7-day annualized yield"),
    ("yield.per_10k", "fund", "daily", "currency_cny", "Fund Yield", "万份收益", "Income per 10k units"),
    # Exchange-traded OHLCV (ETF/LOF)
    ("price.open", "fund", "daily", "currency_cny", "Price Data", "开盘价", "Open price"),
    ("price.close", "fund", "daily", "currency_cny", "Price Data", "收盘价", "Close price"),
    ("price.high", "fund", "daily", "currency_cny", "Price Data", "最高价", "High price"),
    ("price.low", "fund", "daily", "currency_cny", "Price Data", "最低价", "Low price"),
    ("price.volume", "fund", "daily", "shares", "Trading Data", "成交量", "Volume"),
    ("price.amount", "fund", "daily", "currency_cny", "Trading Data", "成交额", "Turnover"),
    # Scale
    ("aum", "fund", "quarterly", "亿元", "Fund Scale", "资产规模", "Assets under management"),
    # Trailing returns (daily as-of snapshots, xueqiu)
    ("return.1w", "fund", "daily", "%", "Fund Performance", "近1周收益率", "Return, 1 week"),
    ("return.1m", "fund", "daily", "%", "Fund Performance", "近1月收益率", "Return, 1 month"),
    ("return.3m", "fund", "daily", "%", "Fund Performance", "近3月收益率", "Return, 3 months"),
    ("return.6m", "fund", "daily", "%", "Fund Performance", "近6月收益率", "Return, 6 months"),
    ("return.1y", "fund", "daily", "%", "Fund Performance", "近1年收益率", "Return, 1 year"),
    ("return.3y", "fund", "daily", "%", "Fund Performance", "近3年收益率", "Return, 3 years"),
    ("return.since_inception", "fund", "daily", "%", "Fund Performance", "成立以来收益率", "Return since inception"),
    # Holder structure / rating
    ("holder.institutional_ratio", "fund", "semiannual", "%", "Holder Structure", "机构持有比例", "Institutional holder ratio"),
    ("rating.stars", "fund", "irregular", "stars", "Fund Rating", "基金评级(星)", "Star rating"),
]

PERSON_CONCEPTS: list[tuple[str, str, str, str, str, str, str]] = [
    ("aum_total", "person", "daily", "亿元", "Manager Stats", "现任基金资产总规模", "Total AUM of current funds"),
    ("funds_count", "person", "daily", "units", "Manager Stats", "现任基金数量", "Number of current funds"),
    ("tenure_days", "person", "daily", "days", "Manager Stats", "累计从业时间", "Tenure, days"),
    ("best_return", "person", "daily", "%", "Manager Stats", "现任基金最佳回报", "Best return among current funds"),
]


def _upsert_concept(session: Session, row: tuple, dry_run: bool) -> str:
    code, entity_type, frequency, unit, category, name_zh, name_en = row
    key = dict(code=code, entity_type=entity_type, measure="", unit=unit, frequency=frequency)
    existing = session.query(Concept).filter_by(**key).first()
    if existing is not None:
        changed = False
        for attr, val in (("category", category), ("name_zh", name_zh), ("name_en", name_en)):
            if getattr(existing, attr) != val:
                setattr(existing, attr, val)
                changed = True
        return "updated" if changed else "exists"
    c = Concept(**key, category=category, name_zh=name_zh, name_en=name_en,
                source=None, verified=True)
    if not dry_run:
        session.add(c)
        session.flush()
    return "created"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db-url", default=os.environ.get("FD_OPEN_DATA_MCP_DATABASE_URL", DEFAULT_URL))
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    eng = create_engine(args.db_url)
    SF = sessionmaker(bind=eng)
    stats = {"created": 0, "updated": 0, "exists": 0}
    with SF() as session:
        for row in FUND_CONCEPTS + PERSON_CONCEPTS:
            stats[_upsert_concept(session, row, args.dry_run)] += 1
        if args.dry_run:
            session.rollback()
        else:
            session.commit()
    total = sum(stats.values())
    print(f"done: {total} concepts — {stats['created']} created, {stats['updated']} updated, "
          f"{stats['exists']} unchanged" + (" (dry-run, rolled back)" if args.dry_run else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())

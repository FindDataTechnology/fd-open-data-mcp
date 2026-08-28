"""Mark bulk_snapshot functions in the registry (task 6.4,
fix-silent-zero-yield-crawls).

Sets functions.bulk_snapshot = True for the cross-section endpoints verified
reachable from the crawl cluster (single calls returning 3,653–23,897 rows):

    stock_zcfz_em              datacenter.eastmoney.com   5,166 rows
    stock_lrb_em               datacenter.eastmoney.com   5,236 rows
    stock_yjbb_em              datacenter.eastmoney.com   6,026 rows
    stock_fhps_em              datacenter.eastmoney.com   3,653 rows
    fund_open_fund_daily_em    fund.eastmoney.com        23,897 rows
    fund_open_fund_rank_em     fund.eastmoney.com        20,176 rows
    fund_rating_all            fund.eastmoney.com        18,070 rows

With the flag set, the planner collapses a concept bound to one of these to a
single cell per date (snapshot-first, design D6) instead of one cell per
entity. Idempotent; run AFTER alembic migration 006.

Usage:
    FD_OPEN_DATA_MCP_DATABASE_URL=postgres://... \
        python scripts/mark_bulk_snapshot.py [--dry-run]
"""
from __future__ import annotations

import sys

from fd_open_data_mcp.db import get_database
from fd_open_data_mcp.models import Function, Source

SNAPSHOT_COMMANDS = [
    "stock_zcfz_em",
    "stock_lrb_em",
    "stock_yjbb_em",
    "stock_fhps_em",
    "fund_open_fund_daily_em",
    "fund_open_fund_rank_em",
    "fund_rating_all",
]


def main() -> int:
    args = sys.argv[1:]
    dry = "--dry-run" in args
    session = get_database().get_session()
    try:
        marked = skipped = 0
        q = (session.query(Function)
             .join(Source, Function.source_id == Source.id)
             .filter(Function.command.in_(SNAPSHOT_COMMANDS),
                     Source.name == "akshare"))
        for fn in q.all():
            if fn.bulk_snapshot:
                skipped += 1
                continue
            print(f"mark {fn.command} (function {fn.id}) bulk_snapshot=True"
                  f"{'  [dry-run]' if dry else ''}")
            if not dry:
                fn.bulk_snapshot = True
            marked += 1
        if not dry:
            session.commit()
        print(f"marked={marked} already-flagged={skipped} "
              f"(commands without a registry row are NOT created here — seed "
              f"bindings separately)")
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    sys.exit(main())

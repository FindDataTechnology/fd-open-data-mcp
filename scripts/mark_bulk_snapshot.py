"""Mark bulk_snapshot functions in the registry (task 6.4,
fix-silent-zero-yield-crawls).

Sets functions.bulk_snapshot = True for cross-section endpoints verified
reachable from the crawl cluster. A snapshot is identified by its full
cross-section response shape, not by a fixed maximum row count:

    stock_zcfz_em              datacenter.eastmoney.com   5,166 rows
    stock_lrb_em               datacenter.eastmoney.com   5,236 rows
    stock_yjbb_em              datacenter.eastmoney.com   6,026 rows
    stock_fhps_em              datacenter.eastmoney.com   3,653 rows
    fund_open_fund_daily_em    fund.eastmoney.com        23,897 rows
    fund_open_fund_rank_em     fund.eastmoney.com        20,176 rows
    fund_rating_all            fund.eastmoney.com        18,070 rows
    fund_manager_em            fund.eastmoney.com        ~35,000 rows

With the flag set, the planner collapses a concept bound to one of these to a
single cell per date (snapshot-first, design D6) instead of one cell per
entity. Idempotent; run AFTER alembic migration 006.

Usage:
    FD_OPEN_DATA_MCP_DATABASE_URL=postgres://... \
        python scripts/mark_bulk_snapshot.py [--dry-run]
"""
from __future__ import annotations

import sys

from fd_open_data_mcp.scripts.mark_bulk_snapshot import main


if __name__ == "__main__":
    sys.exit(main())

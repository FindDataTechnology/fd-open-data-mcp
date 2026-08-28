"""Migrate frozen-window crawl policies to rolling windows (task 4.2,
fix-silent-zero-yield-crawls).

Five of six live policies use date_policy {mode: explicit, end: <past date>} —
windows that were fully crawled days ago, so every cron tick re-fetches a
complete range and upserts nothing (root cause R1). The reconciler now REFUSES
such policies on the recurring path; this script converts them so the fleet
keeps running instead of alerting forever.

Conversion: explicit -> since_last (watermark-driven: each tick fetches from
the per-concept observation watermark, i.e. only new dates). Pass
--trailing-days N to use {mode: trailing, days: N} instead (re-crawl the last
N days every tick — safer for sources with late revisions).

Usage (against the master control-plane DB):
    FD_OPEN_DATA_MCP_DATABASE_URL=postgres://... \
        python scripts/migrate_frozen_policies.py [--dry-run] [--trailing-days 3]
"""
from __future__ import annotations

import sys

from fd_open_data_mcp.db import get_database
from fd_open_data_mcp.models import CrawlPolicy


def main() -> int:
    args = sys.argv[1:]
    dry = "--dry-run" in args
    trailing_days = None
    if "--trailing-days" in args:
        trailing_days = int(args[args.index("--trailing-days") + 1])

    session = get_database().get_session()
    try:
        frozen = [p for p in session.query(CrawlPolicy).all()
                  if (p.date_policy or {}).get("mode") == "explicit"]
        if not frozen:
            print("no explicit-window policies found; nothing to do")
            return 0
        for p in frozen:
            new_dp = ({"mode": "trailing", "days": trailing_days}
                      if trailing_days is not None else {"mode": "since_last"})
            print(f"policy {p.id} ({p.name}): {p.date_policy} -> {new_dp}"
                  f"{'  [dry-run]' if dry else ''}")
            if not dry:
                p.date_policy = new_dp
        if not dry:
            session.commit()
            print(f"migrated {len(frozen)} policies (committed)")
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    sys.exit(main())

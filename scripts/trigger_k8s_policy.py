"""Trigger-now a crawl policy on the in-cluster fd-open-pg via K8sJobLauncher.

Targets the cluster (kubectl from this Mac -> cluster). The reconciler CronJob
will pick up due policies the same way; this lets us launch a specific policy
for the 7.5 failover validation without waiting for the cron.

Usage:
    FD_OPEN_DATA_MCP_DATABASE_URL='postgresql+psycopg2://postgres:admin123@127.0.0.1:55432/postgres' \
    RECONCILER_LAUNCHER=k8s \
    python scripts/trigger_k8s_policy.py <policy_id> [--due-cron HH:MM]
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import sys

from fd_open_data_mcp.db import get_database
from fd_open_data_mcp.models import CrawlPolicy
from fd_open_data_mcp.refresh.reconciler import launch_policy


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("policy_id", type=int)
    ap.add_argument("--db", default=os.environ.get(
        "FD_OPEN_DATA_MCP_DATABASE_URL",
        "postgresql+psycopg2://postgres:admin123@127.0.0.1:55432/postgres"))
    ap.add_argument("--now", default=None, help="override 'now' (YYYY-MM-DDTHH:MM) for cron-due test")
    args = ap.parse_args()

    s = get_database().get_session()
    try:
        p = s.query(CrawlPolicy).get(args.policy_id)
        if not p:
            print(f"policy {args.policy_id} not found"); return 1
        print(f"policy: {p.name} | cron={p.cron_expr} tz={p.timezone} "
              f"concepts={p.concept_ids} mode={p.mode} enabled={p.enabled}")
        now = dt.datetime.now(dt.timezone.utc)
        if args.now:
            now = dt.datetime.fromisoformat(args.now).replace(tzinfo=dt.timezone.utc)
        result = launch_policy(s, p, _make_launcher(), now=now)
        print("result:", result)
        return 0 if result.get("status") == "launched" else 2
    finally:
        s.close()


def _make_launcher():
    from fd_open_data_mcp.refresh.reconciler import K8sJobLauncher
    return K8sJobLauncher()


if __name__ == "__main__":
    raise SystemExit(main())

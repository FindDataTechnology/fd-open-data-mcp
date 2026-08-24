"""Crawl visibility (add-crawl-visibility).

A read-only watcher that surfaces what the scraw system did, is doing, and
will do. Two entrypoints:

- ``python -m fd_open_data_mcp.visibility.scan``   — failure + stale-run scan
  (k8s CronJob, ~15 min, offset after the reconciler tick).
- ``python -m fd_open_data_mcp.visibility.digest`` — datasource-centric daily
  digest (k8s CronJob, 08:00 Asia/Shanghai).

Both READ the control-plane tables (``policy_runs``, ``fetch_log``,
``crawl_policies``, ``clusters``, ``source_proxy_health``) and never mutate
crawl state. Dedup watermarks live in Redis. Notifications go out through a
pluggable ``Notifier`` sink (v1 = ServerChan → WeChat).

See ``openspec/changes/add-crawl-visibility`` for the full design.
"""
from __future__ import annotations

from fd_open_data_mcp.visibility.notifiers.base import Notifier
from fd_open_data_mcp.visibility.notifiers.factory import get_notifier

__all__ = ["Notifier", "get_notifier"]

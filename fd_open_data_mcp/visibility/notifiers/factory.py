"""Notifier factory (add-crawl-visibility).

Picks the transport at runtime from ``SCRAW_NOTIFIER`` (default ``wechat``).
Unknown values degrade to a no-op ``NullNotifier`` that logs the notification
instead of crashing the watcher — alerting misconfiguration must never break a
scan/digest tick. Adding a sink = a new module + one branch here; the watcher
logic is unchanged.
"""
from __future__ import annotations

import logging
import os

from fd_open_data_mcp.visibility.notifiers.base import Notifier
from fd_open_data_mcp.visibility.notifiers.wechat import NullNotifier, ServerChanNotifier  # noqa: F401

# ``NullNotifier`` is re-exported so tests can import the no-op sink from the
# canonical factory module without reaching into the transport module.

logger = logging.getLogger(__name__)

_SINKS = {"wechat": ServerChanNotifier, "serverchan": ServerChanNotifier}


def get_notifier(name: str | None = None) -> Notifier:
    """Return the configured notifier sink (default wechat/ServerChan)."""
    kind = (name or os.environ.get("SCRAW_NOTIFIER") or "wechat").strip().lower()
    if kind in ("none", "null", "off", "dry-run"):
        return NullNotifier()
    cls = _SINKS.get(kind)
    if cls is None:
        logger.warning(
            "unknown SCRAW_NOTIFIER=%r — falling back to NullNotifier "
            "(set SCRAW_NOTIFIER to one of: %s)", kind, ", ".join(sorted(_SINKS)),
        )
        return NullNotifier()
    return cls()

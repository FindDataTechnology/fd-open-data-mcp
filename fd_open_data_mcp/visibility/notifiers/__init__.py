"""Notifier sinks for the crawl watcher (add-crawl-visibility).

A sink implements ``Notifier.send(title, body, *, level)``. The scan/digest
entrypoints depend only on the interface, never on a transport, so additional
sinks (Telegram, email, generic webhook) are addable without touching watcher
logic. See ``factory.get_notifier``.
"""
from __future__ import annotations

from fd_open_data_mcp.visibility.notifiers.base import Notifier

__all__ = ["Notifier"]

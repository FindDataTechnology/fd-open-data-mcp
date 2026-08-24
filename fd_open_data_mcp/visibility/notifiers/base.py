"""Notifier sink interface (add-crawl-visibility).

The scan and digest entrypoints call ``Notifier.send`` and never import a
specific transport, so the delivery channel (ServerChan/Telegram/email/…)
is swappable via the ``SCRAW_NOTIFIER`` env without watcher-logic changes.
"""
from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class Notifier(Protocol):
    """A push-notification sink.

    ``send`` MUST NOT raise on a delivery failure — the watcher must stay
    healthy and keep advancing its watermark even when a sink is down. A sink
    that cannot deliver (missing token, network, bad config) logs and no-ops.
    """

    def send(self, title: str, body: str, *, level: str = "info") -> None: ...



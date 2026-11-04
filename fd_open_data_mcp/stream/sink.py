"""Producer/consumer contract for the stream plane (task 3.1).

Two roles, two interfaces:

  - ``EventSink``   the producer half a worker holds (publish)
  - ``EventStream`` the consumer half the ingester holds (read/ack/reclaim/
                     pending/iter_range)

A backend (Redis Streams today, Kafka as a future profile) implements both
behind these ABCs. The stream-plane spec's pinned behaviors — at-least-once
delivery, idempotent landing, replay by time range, health visibility — are
expressed ONLY through this contract, so swapping the backend cannot change
them (the same contract test suite runs against every implementation).

Delivery semantics: ``read`` hands out messages to a consumer in a group;
they stay pending until ``ack``; ``reclaim`` (min-idle takeover) gives a
crashed consumer's messages to another — that is the at-least-once half. The
idempotent half lives in the landing (``ingest`` reuses the direct-write
upsert), not here: a backend may deliver the same message twice and the store
stays correct.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from fd_open_data_mcp.stream.envelope import EventEnvelope


@dataclass(frozen=True)
class StreamMessage:
    """One delivered message: the backend's entry id + the decoded envelope."""

    entry_id: str
    envelope: EventEnvelope


@dataclass(frozen=True)
class PendingSummary:
    """Consumer-group health (the stream-plane「消费滞后与积压可见」contract).

    ``length``: total entries in the stream; ``pending``: delivered-but-unacked
    count; ``oldest_pending_idle_ms``: age of the oldest unacked message (None
    when nothing pending) — the lag signal; ``consumers``: live consumer count.
    """

    length: int
    pending: int
    oldest_pending_idle_ms: int | None
    consumers: int


class EventSink(ABC):
    """Producer side."""

    @abstractmethod
    def publish(self, events: list[EventEnvelope]) -> int:
        """Publish a batch; returns the number accepted. A failure raises —
        the caller decides the degradation (direct write), never swallows."""

    @abstractmethod
    def ensure_stream(self) -> None:
        """Create the stream/group infrastructure if absent (idempotent)."""

    def close(self) -> None:  # pragma: no cover - default no-op
        pass


class EventStream(ABC):
    """Consumer side."""

    @abstractmethod
    def ensure_group(self, group: str) -> None:
        """Create the consumer group if absent (idempotent)."""

    @abstractmethod
    def read(self, group: str, consumer: str, *, count: int = 100,
             block_ms: int = 0) -> list[StreamMessage]:
        """Deliver up to ``count`` new messages to ``consumer`` in ``group``.
        block_ms=0 returns immediately (no new messages → empty list)."""

    @abstractmethod
    def ack(self, group: str, entry_ids: list[str]) -> int:
        """Mark delivered messages processed; returns count acked."""

    @abstractmethod
    def reclaim(self, group: str, consumer: str, *, min_idle_ms: int,
                count: int = 100) -> list[StreamMessage]:
        """Take over messages idle longer than ``min_idle_ms`` (crashed
        consumers) and redeliver to ``consumer`` — the at-least-once half."""

    @abstractmethod
    def pending_summary(self, group: str) -> PendingSummary:
        """Health snapshot for the group (backlog/lag/consumers)."""

    @abstractmethod
    def iter_range(self, *, start_ms: int | None = None,
                   end_ms: int | None = None,
                   max_count: int = 10_000) -> list[StreamMessage]:
        """Messages whose backend entry id falls in [start_ms, end_ms] — the
        replay cursor. Time-bounded because Redis stream ids are ms-based."""

    def close(self) -> None:  # pragma: no cover - default no-op
        pass

"""In-process EventSink/EventStream (task 3.1).

Two uses:

  1. The contract test double — the same contract suite runs against this and
     the Redis backend, proving the backend is swappable without changing
     behavior (stream-plane「默认安装不引入重量级消息中间件」scenario).
  2. A dev/single-process mode where no Redis is configured.

It is NOT the production degradation path: when Redis is down the worker
falls back to DIRECT WRITE (see ingest/worker hook), not to an in-memory
buffer — an in-memory buffer loses events on process exit, which the
degradation contract forbids ("MUST NOT 造成数据丢失").

Consumer-group semantics are modeled faithfully: per-group read cursor + a
pending (delivered-unacked) map, so read/ack/reclaim behave like Redis
consumer groups, including at-least-once redelivery of unacked messages.
"""
from __future__ import annotations


import threading
import time

from fd_open_data_mcp.stream.envelope import EventEnvelope
from fd_open_data_mcp.stream.sink import (
    EventSink, EventStream, PendingSummary, StreamMessage,
)


class InMemoryStream(EventSink, EventStream):
    """Thread-safe in-memory stream with consumer-group semantics."""

    def __init__(self, name: str = "observations") -> None:
        self._name = name
        self._lock = threading.RLock()
        # entry_id (str "<ms>-<seq>") -> envelope
        self._entries: list[tuple[str, EventEnvelope]] = []
        self._seq = 0
        self._last_ms = -1
        # group -> {cursor_index, pending: {entry_id: (consumer, delivered_at_ms)}}
        self._groups: dict[str, dict] = {}

    # -- producer -----------------------------------------------------------
    def ensure_stream(self) -> None:
        pass  # the list always exists

    def publish(self, events: list[EventEnvelope]) -> int:
        with self._lock:
            for ev in events:
                entry_id = self._next_entry_id()
                self._entries.append((entry_id, ev))
            return len(events)

    def _next_entry_id(self) -> str:
        ms = int(time.time() * 1000)
        if ms == self._last_ms:
            self._seq += 1
        else:
            self._last_ms = ms
            self._seq = 0
        return f"{ms}-{self._seq}"

    # -- consumer -----------------------------------------------------------
    def _group(self, group: str) -> dict:
        return self._groups.setdefault(group, {"cursor": 0, "pending": {}})

    def ensure_group(self, group: str) -> None:
        with self._lock:
            self._group(group)

    def read(self, group: str, consumer: str, *, count: int = 100,
             block_ms: int = 0) -> list[StreamMessage]:
        with self._lock:
            g = self._group(group)
            out: list[StreamMessage] = []
            # new messages past the cursor
            i = g["cursor"]
            while i < len(self._entries) and len(out) < count:
                entry_id, ev = self._entries[i]
                out.append(StreamMessage(entry_id, ev))
                g["pending"][entry_id] = (consumer, int(time.time() * 1000))
                i += 1
            g["cursor"] = i
            return out

    def ack(self, group: str, entry_ids: list[str]) -> int:
        with self._lock:
            g = self._group(group)
            n = 0
            for eid in entry_ids:
                if g["pending"].pop(eid, None) is not None:
                    n += 1
            return n

    def reclaim(self, group: str, consumer: str, *, min_idle_ms: int,
                count: int = 100) -> list[StreamMessage]:
        with self._lock:
            g = self._group(group)
            now = int(time.time() * 1000)
            by_id = {eid: ev for eid, ev in self._entries}
            out: list[StreamMessage] = []
            for eid, (owner, delivered_ms) in list(g["pending"].items()):
                if len(out) >= count:
                    break
                if now - delivered_ms >= min_idle_ms:
                    out.append(StreamMessage(eid, by_id[eid]))
                    g["pending"][eid] = (consumer, now)  # reassigned
            return out

    def pending_summary(self, group: str) -> PendingSummary:
        with self._lock:
            g = self._group(group)
            now = int(time.time() * 1000)
            oldest = None
            if g["pending"]:
                oldest = now - min(d for _, d in g["pending"].values())
            return PendingSummary(
                length=len(self._entries),
                pending=len(g["pending"]),
                oldest_pending_idle_ms=oldest,
                consumers=len({c for c, _ in g["pending"].values()}),
            )

    def iter_range(self, *, start_ms: int | None = None,
                   end_ms: int | None = None,
                   max_count: int = 10_000) -> list[StreamMessage]:
        with self._lock:
            out: list[StreamMessage] = []
            for entry_id, ev in self._entries:
                ms = int(entry_id.split("-")[0])
                if start_ms is not None and ms < start_ms:
                    continue
                if end_ms is not None and ms > end_ms:
                    continue
                out.append(StreamMessage(entry_id, ev))
                if len(out) >= max_count:
                    break
            return out

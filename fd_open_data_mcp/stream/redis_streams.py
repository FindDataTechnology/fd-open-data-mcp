"""Redis Streams backend (task 3.2) — the default stream plane.

Implements EventSink + EventStream over Redis Streams consumer groups:

  publish      XADD (maxlen cap → approximate trimming, bounded memory)
  ensure_group XGROUP CREATE (id='0' so a fresh group sees history)
  read         XREADGROUP  >
  ack          XACK
  reclaim      XAUTOCLAIM  (crashed-consumer takeover → at-least-once)
  pending      XPENDING + XINFO GROUPS (backlog / oldest idle / consumers)
  iter_range   XRANGE by ms entry-id (replay cursor)

Redis is the only heavy dependency and it is already a declared install dep
(``redis>=5.0``) — the lightweight-default requirement holds without a new
package. The client is injected (constructor) so the contract test drives it
with a stateful fake and production uses the shared lazy client.

Entry ids are Redis-assigned ``<ms>-<seq>``; the envelope's own ULID is
carried in the fields as the idempotency key (dedup happens at landing, not
here). decode_responses=True is assumed on the client (the project's redis
convention).
"""
from __future__ import annotations

from fd_open_data_mcp.stream.envelope import EventEnvelope
from fd_open_data_mcp.stream.sink import (
    EventSink, EventStream, PendingSummary, StreamMessage,
)


class RedisStreamsSink(EventSink, EventStream):
    def __init__(self, client, *, stream: str = "fdodm:observations",
                 maxlen: int | None = 1_000_000, approximate: bool = True):
        self._r = client
        self._stream = stream
        self._maxlen = maxlen
        self._approx = approximate

    # -- producer -----------------------------------------------------------
    def ensure_stream(self) -> None:
        # XADD with a sentinel then delete? No — the stream materializes on
        # first XADD; nothing to pre-create. Group creation is what needs the
        # stream to exist, handled by ensure_group's mkstream.
        pass

    def publish(self, events: list[EventEnvelope]) -> int:
        pipe = self._r.pipeline(transaction=False)
        for ev in events:
            pipe.xadd(self._stream, ev.to_fields(),
                      maxlen=self._maxlen, approximate=self._approx)
        pipe.execute()
        return len(events)

    # -- consumer -----------------------------------------------------------
    def ensure_group(self, group: str) -> None:
        try:
            # mkstream=True creates the stream if absent so a fresh group
            # works even before any publish.
            self._r.xgroup_create(self._stream, group, id="0", mkstream=True)
        except Exception as e:  # noqa: BLE001
            if "BUSYGROUP" not in str(e):
                raise

    def read(self, group: str, consumer: str, *, count: int = 100,
             block_ms: int = 0) -> list[StreamMessage]:
        resp = self._r.xreadgroup(
            group, consumer, {self._stream: ">"},
            count=count, block=block_ms or None)
        return self._decode(resp)

    def ack(self, group: str, entry_ids: list[str]) -> int:
        if not entry_ids:
            return 0
        return int(self._r.xack(self._stream, group, *entry_ids))

    def reclaim(self, group: str, consumer: str, *, min_idle_ms: int,
                count: int = 100) -> list[StreamMessage]:
        # XAUTOCLAIM returns (next_cursor, [messages], [deleted]); redis-py
        # surfaces it as (cursor, [(id, fields), ...]) or a 3-tuple depending
        # on version — normalize by taking the message list.
        resp = self._r.xautoclaim(self._stream, group, consumer,
                                  min_idle_time=min_idle_ms, count=count)
        if not resp:
            return []
        msgs = resp[1] if isinstance(resp, (list, tuple)) and len(resp) >= 2 else resp
        return [StreamMessage(eid, EventEnvelope.from_fields(fields))
                for eid, fields in msgs]

    def pending_summary(self, group: str) -> PendingSummary:
        length = int(self._r.xlen(self._stream))
        info = self._r.xpending(self._stream, group) or {}
        pending = int(info.get("pending", 0)) if isinstance(info, dict) else 0
        # redis-py surfaces `consumers` as a list of per-consumer dicts (≥5.x)
        # or an int depending on version — normalize to a count.
        cons = info.get("consumers", 0) if isinstance(info, dict) else 0
        consumers = len(cons) if isinstance(cons, list) else int(cons or 0)
        oldest = None
        if pending:
            # XPENDING range detail, oldest first (min='-'), gives the exact
            # idle age without reassigning anything.
            peek = self._r.xpending_range(self._stream, group, min="-", max="+",
                                          count=1)
            if peek:
                oldest = int(peek[0].get("time_since_delivered", 0))
        return PendingSummary(length=length, pending=pending,
                              oldest_pending_idle_ms=oldest, consumers=consumers)

    def iter_range(self, *, start_ms: int | None = None,
                   end_ms: int | None = None,
                   max_count: int = 10_000) -> list[StreamMessage]:
        lo = f"{start_ms}-0" if start_ms is not None else "-"
        hi = f"{end_ms}-999999999" if end_ms is not None else "+"
        resp = self._r.xrange(self._stream, min=lo, max=hi, count=max_count)
        return [StreamMessage(eid, EventEnvelope.from_fields(fields))
                for eid, fields in resp]

    @staticmethod
    def _decode(resp) -> list[StreamMessage]:
        # XREADGROUP → [(stream_name, [(id, fields), ...]), ...] or empty.
        out: list[StreamMessage] = []
        if not resp:
            return out
        for _name, entries in resp:
            for eid, fields in entries:
                out.append(StreamMessage(eid, EventEnvelope.from_fields(fields)))
        return out

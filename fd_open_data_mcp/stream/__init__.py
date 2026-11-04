"""Stream plane: event transport between crawl workers and storage.

add-data-stream-engine P2. Workers publish normalized records as events; an
ingester consumes, lands them idempotently (sharing the direct write path's
upsert + rollup caliber), and acks. This decouples the fetch side from the
store side (backpressure, replay, independent scaling) while keeping the
data semantics identical to the direct-write path.

Modules:
  - ``envelope``  the event record + ULID idempotency key
  - ``sink``      the producer/consumer contract (EventSink / EventStream)
  - ``memory``    in-process sink — the no-Redis fallback and the test double
  - ``redis_streams``  the default backend (Redis Streams + consumer groups)
  - ``ingest``    the consumer service (land + ack)
  - ``replay``    re-delivery by time/event range (idempotent, cancellable)
  - ``health``    backlog / oldest-pending age / consumer status

The backend is swappable behind ``sink`` without changing any behavior the
stream-plane spec pins (at-least-once, idempotent landing, replay, health).
"""
from __future__ import annotations

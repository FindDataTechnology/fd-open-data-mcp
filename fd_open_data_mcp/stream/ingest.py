"""Ingester: consume events, land them idempotently, ack (task 3.3).

The consumer half of the stream plane. It reads a batch from an EventStream,
groups the records by their landing key (concept, entity, source, granularity),
and lands each group through the SAME ``write_cache_range`` the direct path
uses — so an event and a direct write of the same point produce byte-identical
rows and rollup state (the spec's "两条路径先后到达 SHALL NOT 产生重复行" and
the e2e "event path == direct path" verification).

Idempotency is the natural-key upsert, not a separate event table: re-
delivering an event lands the same (point, source) row again (an update, not a
duplicate), and the rollup hook counts only INSERTs, so redelivery never
double-counts. That is why the ingester can be at-least-once without a dedup
ledger.

A batch is landed then acked together; a landing failure raises BEFORE ack so
the messages stay pending and are reclaimed by another consumer — at-least-
once with no loss.
"""
from __future__ import annotations

from collections import defaultdict

from sqlalchemy.orm import Session

from fd_open_data_mcp.fetch.cache import write_cache_range
from fd_open_data_mcp.stream.envelope import EventEnvelope
from fd_open_data_mcp.stream.sink import EventStream


def _group_key(ev: EventEnvelope):
    return (ev.concept_id, ev.entity_type, ev.entity_id, ev.source_used,
            ev.granularity)


def land_events(session: Session, messages) -> int:
    """Land a list of StreamMessage via the shared idempotent upsert path.

    Used by both the consumer (``ingest_batch``) and the replay tool — the
    spec's "重放 SHALL 复用与正常消费相同的幂等落库路径". Returns rows landed.
    """
    groups: dict[tuple, list[tuple[str, EventEnvelope]]] = defaultdict(list)
    for m in messages:
        groups[_group_key(m.envelope)].append((m.entry_id, m.envelope))
    landed = 0
    for (_cid, _et, _eid, _src, _gran), items in groups.items():
        rows = {ev.date: ev.value for _, ev in items}
        unit = items[0][1].unit
        landed += write_cache_range(session, _cid, _et, _eid, rows, unit, _src,
                                    granularity=_gran)
    return landed


def ingest_batch(session: Session, stream: EventStream, *, group: str,
                 consumer: str, count: int = 500) -> int:
    """Read up to ``count`` messages, land them, ack. Returns rows landed.

    Empty read → 0, no commit. Landing runs in one transaction per group
    (write_cache_range commits per group); ack is per-message so a partial
    ack never strands a landed message.
    """
    msgs = stream.read(group, consumer, count=count)
    if not msgs:
        return 0
    landed = land_events(session, msgs)
    stream.ack(group, [m.entry_id for m in msgs])
    return landed


def run_ingest_loop(session: Session, stream: EventStream, *, group: str,
                    consumer: str, max_batches: int | None = None,
                    block_ms: int = 0) -> int:
    """Drain the stream in batches (a service entrypoint). ``max_batches=None``
    runs until the stream is empty (one pass); a number bounds the loop. Returns
    total rows landed. A real deployment wraps this in a long-lived worker."""
    total = 0
    batches = 0
    while max_batches is None or batches < max_batches:
        n = ingest_batch(session, stream, group=group, consumer=consumer)
        batches += 1
        if n == 0:
            break
        total += n
    return total

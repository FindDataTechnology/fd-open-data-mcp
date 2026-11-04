"""Replay tool (task 3.5): re-deliver events by time/event range.

Backfill and failure recovery used to mean "re-run the crawl policy" — a full
fetch round trip. With the stream plane, replay re-lands events already in the
stream, reusing the ingester's idempotent landing path (``land_events``), so a
replay is a pure storage-side operation: no upstream fetch, and re-landing an
already-landed point is a no-op (natural-key upsert).

Progress is observable and the run is cancellable: ``replay`` walks the range
in chunks, landing each, and checks ``should_cancel`` (a callable) between
chunks — an operator can stop a replay mid-flight without corrupting state
(each chunk commits atomically). The returned summary carries counts so the
panel can show progress.

The range is a [start_ms, end_ms] window over the backend's ms entry ids (see
``EventStream.iter_range``); event_id (ULID) filtering is available via
``event_ids`` for point-targeted replay.
"""
from __future__ import annotations

from typing import Callable, Iterable

from sqlalchemy.orm import Session

from fd_open_data_mcp.stream.ingest import land_events
from fd_open_data_mcp.stream.sink import EventStream, StreamMessage


def replay(
    session: Session, stream: EventStream, *,
    start_ms: int | None = None, end_ms: int | None = None,
    chunk_size: int = 1000, max_rows: int | None = None,
    should_cancel: Callable[[], bool] | None = None,
    progress: Callable[[dict], None] | None = None,
) -> dict:
    """Re-land events in [start_ms, end_ms]. Returns a summary dict.

    ``should_cancel`` is polled between chunks; when it returns True the loop
    stops cleanly (already-landed chunks stay committed — idempotent, so a
    later resume re-lands them harmlessly). ``progress`` receives
    ``{replayed, chunks, cancelled}`` after each chunk for live observation.
    """
    msgs = stream.iter_range(start_ms=start_ms, end_ms=end_ms,
                             max_count=max_rows or 10_000_000)
    replayed = 0
    chunks = 0
    cancelled = False
    for i in range(0, len(msgs), chunk_size):
        if should_cancel is not None and should_cancel():
            cancelled = True
            break
        chunk = msgs[i:i + chunk_size]
        replayed += land_events(session, chunk)
        chunks += 1
        if progress is not None:
            progress({"replayed": replayed, "chunks": chunks,
                      "cancelled": cancelled})
    return {"status": "cancelled" if cancelled else "done",
            "replayed": replayed, "chunks": chunks,
            "scanned": len(msgs)}


def replay_events(session: Session, stream: EventStream,
                  messages: Iterable[StreamMessage]) -> dict:
    """Re-land an explicit message set (point-targeted replay)."""
    n = land_events(session, list(messages))
    return {"status": "done", "replayed": n}

"""Realtime push channel (add-data-stream-engine task 4.5).

For `realtime`-cadence concepts the 5-minute cron poll is replaced by a push
listener: the upstream delivers data (webhook POST, SSE record, ...) and the
listener turns it into events on the stream plane — landing in seconds, not
minutes. The cron stays as the reconciliation backstop (the existing
gapfill/reconciler discovers and fills any push outage; design D5), so a
dropped push is eventually corrected, and the same point delivered by both
paths lands once (natural-key idempotency).

This module is transport-agnostic on purpose: `handle_push` takes already-decoded
records (the webhook route / SSE reader / CLI adapter upstream of it does the
parsing per source — "按源适配"). It reuses the worker-exit publish primitive,
so push and crawl share the exact same event + degradation semantics.

Idempotency note: each push mints fresh event_ids (ULIDs), but the landing
dedups on the (concept, entity, date, granularity, source) natural key — so a
duplicate push of the same point is an update, never a second row. That is why
"断流恢复后无重复无遗漏" holds without a dedup ledger.
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from fd_open_data_mcp.stream.publish import emit_observations
from fd_open_data_mcp.stream.sink import EventSink


def _normalize(source: str, rec: dict) -> dict:
    """Fill the fields emit_observations requires from a push record.

    A push payload carries the observation point + value; the source and a
    default granularity/unit are supplied by the adapter. Unknown shapes raise
    (a malformed push is a loud error, never a silent partial ingest — the
    concept-fetch failure caliber).
    """
    try:
        return {
            "concept_id": rec["concept_id"],
            "entity_type": rec["entity_type"],
            "entity_id": rec["entity_id"],
            "date": rec["date"],
            "granularity": rec.get("granularity", "day"),
            "source_used": rec.get("source_used", source),
            "value": rec.get("value"),
            "unit": rec.get("unit"),
            "source": source,
            "payload_ref": rec.get("payload_ref"),
        }
    except KeyError as e:
        raise ValueError(f"push record missing field {e}") from e


def handle_push(session: Session, sink: EventSink | None, source: str,
                records: list[dict], *, degrade_to_direct: bool = True) -> dict:
    """Ingest a batch of pushed records via the stream plane.

    ``records``: list of push payloads (concept_id/entity/date/value/...).
    Returns the publish result ({mode, published|rows, error?}) — the caller
    (webhook route / SSE reader) surfaces it as the HTTP response. A sink
    outage degrades to a direct write (no loss), same as the crawl worker.
    """
    normalized = [_normalize(source, r) for r in records]
    return emit_observations(session, sink, normalized,
                             degrade_to_direct=degrade_to_direct)


def handle_single(session: Session, sink: EventSink | None, source: str,
                  record: dict, **kw) -> dict:
    """One pushed observation (the common webhook shape)."""
    return handle_push(session, sink, source, [record], **kw)

"""Worker-exit publish primitive (task 3.4).

The single call site a crawl worker uses to hand its normalized records to
storage. It encapsulates the stream-plane contract's degradation rule:

  - a sink is configured  → publish events (the default write path)
  - publish fails AND degrade_to_direct → fall back to the direct upsert, so
    an outage never loses data (the spec: "降级 MUST NOT 造成数据丢失")
  - no sink configured     → direct write (the pre-stream behavior, kept as
    the escape hatch)

Both paths land through the SAME ``write_cache_range``/``write_cache`` as the
ingester, so a degraded direct write and an event landing are byte-identical
— the degradation switch changes transport, never data semantics.

The result dict carries the ``mode`` so the caller records it on the run
detail (``policy_runs.detail`` / ``crawl_runs``) — the "运行详情标注降级状态"
the spec pins. The sink is injected (the worker builds it from REDIS_URL);
``None`` means "no stream configured, write directly".
"""
from __future__ import annotations

from collections import defaultdict

from sqlalchemy.orm import Session

from fd_open_data_mcp.fetch.cache import write_cache_range
from fd_open_data_mcp.stream.envelope import EventEnvelope, new_event_id
from fd_open_data_mcp.stream.sink import EventSink


def record_to_envelope(rec: dict) -> EventEnvelope:
    return EventEnvelope(
        event_id=new_event_id(),
        source=rec.get("source", ""),
        concept_id=rec["concept_id"],
        entity_type=rec["entity_type"],
        entity_id=rec["entity_id"],
        date=rec["date"],
        granularity=rec.get("granularity", "day"),
        source_used=rec["source_used"],
        value=("" if rec.get("value") is None else str(rec["value"])),
        unit=rec.get("unit"),
        payload_ref=rec.get("payload_ref"),
    )


def _direct_write(session: Session, records: list[dict]) -> int:
    """Group by landing key and upsert via write_cache_range (shared caliber)."""
    groups: dict[tuple, dict] = defaultdict(dict)
    units: dict[tuple, str] = {}
    for rec in records:
        key = (rec["concept_id"], rec["entity_type"], rec["entity_id"],
               rec["source_used"], rec.get("granularity", "day"))
        groups[key][rec["date"]] = rec.get("value")
        units.setdefault(key, rec.get("unit"))
    total = 0
    for key, rows in groups.items():
        cid, et, eid, src, gran = key
        total += write_cache_range(session, cid, et, eid, rows, units[key], src,
                                   granularity=gran)
    return total


def emit_observations(
    session: Session, sink: EventSink | None, records: list[dict], *,
    degrade_to_direct: bool = True,
) -> dict:
    """Hand ``records`` to storage. Returns ``{mode, rows|published, error?}``.

    ``mode`` is one of ``event`` (published), ``direct`` (no sink), or
    ``degraded`` (sink failed → direct write). The caller records it on the
    run detail. A degraded publish that also fails its direct write raises —
    the worker's run then closes failed with the error, never a silent loss.
    """
    if not records:
        return {"mode": "noop", "rows": 0}
    if sink is None:
        return {"mode": "direct", "rows": _direct_write(session, records)}
    try:
        n = sink.publish([record_to_envelope(r) for r in records])
        return {"mode": "event", "published": n}
    except Exception as e:  # noqa: BLE001 - transport failure → degradation
        if not degrade_to_direct:
            raise
        rows = _direct_write(session, records)
        return {"mode": "degraded", "rows": rows, "error": str(e)}

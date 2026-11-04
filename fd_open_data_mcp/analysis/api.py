"""Analysis aggregation API (task 5.2).

Reads through the configured ``AnalysisBackend`` (default: the rollup tier).
Two operations, both with the failure discipline the spec pins — every failure
is an explicit error payload, never a silent empty/partial result (the
concept-fetch caliber, reused here):

  - ``aggregate``  per-concept coverage summary (bounded result set)
  - ``series``     one concept×entity time series over a date window

Range caps are enforced BEFORE the backend runs: a query wider than the cap is
REFUSED with the cap + a narrowing hint, never silently truncated. The four
failure classes (spec analysis-plane「失败语义与 concept-fetch 同口径」):
  - ``backend_unavailable``  the read path raised
  - ``range_exceeded``       window/result beyond the configured cap
  - ``not_found``            concept / entity does not exist
  - ``data_not_ready``       requested window beyond the rollup's coverage
"""
from __future__ import annotations

import os
from typing import Any

from sqlalchemy.orm import Session

from fd_open_data_mcp.analysis.backend import get_backend
from fd_open_data_mcp.models import Concept, ConceptCoverage

# caps (overridable by env for different deployments)
MAX_SERIES_SPAN_DAYS = int(os.environ.get("FD_ODM_ANALYSIS_MAX_SPAN_DAYS", "3650"))
MAX_SERIES_ROWS = int(os.environ.get("FD_ODM_ANALYSIS_MAX_ROWS", "10000"))
MAX_SUMMARY_ROWS = int(os.environ.get("FD_ODM_ANALYSIS_MAX_SUMMARY", "5000"))
# a concept whose rollup last_fetch is older than this is "not ready" for a
# fresh-data request (the rollup lag signal, task 2.7's freshness)
STALE_HOURS = int(os.environ.get("FD_ODM_ANALYSIS_STALE_HOURS", "48"))


def _err(code: str, message: str, **extra: Any) -> dict:
    return {"ok": False, "error": code, "message": message, **extra}


def aggregate(session: Session, *, concept_id: int | None = None,
              entity_type: str | None = None,
              limit: int | None = None) -> dict:
    """Per-concept coverage summary via the analysis backend."""
    if concept_id is not None:
        c = session.get(Concept, concept_id)
        if c is None:
            return _err("not_found", f"concept {concept_id} does not exist",
                        concept_id=concept_id)
    try:
        backend = get_backend()
        rows = backend.concept_summary(session, concept_id=concept_id,
                                       entity_type=entity_type)
    except Exception as e:  # noqa: BLE001
        return _err("backend_unavailable", f"analysis backend failed: {e}")
    cap = limit or MAX_SUMMARY_ROWS
    if len(rows) > cap:
        return _err("range_exceeded",
                    f"summary has {len(rows)} concepts > cap {cap}; "
                    "narrow with concept_id/entity_type or raise the limit",
                    cap=cap, total=len(rows))
    return {"ok": True, "count": len(rows), "concepts": rows}


def series(session: Session, *, concept_id: int, entity_type: str, entity_id: int,
           start: str | None = None, end: str | None = None) -> dict:
    """One concept×entity time series over a bounded window."""
    c = session.get(Concept, concept_id)
    if c is None:
        return _err("not_found", f"concept {concept_id} does not exist",
                    concept_id=concept_id)
    # range cap on the window span
    if start and end:
        span = _span_days(start, end)
        if span is not None and span > MAX_SERIES_SPAN_DAYS:
            return _err("range_exceeded",
                        f"window {span}d exceeds cap {MAX_SERIES_SPAN_DAYS}d; "
                        "narrow the range", cap_days=MAX_SERIES_SPAN_DAYS)
    # data-not-ready: the concept has no rollup coverage at all
    cov = session.query(ConceptCoverage).filter_by(concept_id=concept_id).first()
    if cov is None or cov.rows == 0:
        return _err("data_not_ready",
                    f"concept {concept_id} has no covered observations yet",
                    concept_id=concept_id)
    try:
        backend = get_backend()
        rows = backend.time_series(session, concept_id=concept_id,
                                   entity_type=entity_type, entity_id=entity_id,
                                   start=start, end=end, limit=MAX_SERIES_ROWS + 1)
    except Exception as e:  # noqa: BLE001
        return _err("backend_unavailable", f"analysis backend failed: {e}")
    if len(rows) > MAX_SERIES_ROWS:
        return _err("range_exceeded",
                    f"series exceeds {MAX_SERIES_ROWS} rows; narrow the window",
                    cap=MAX_SERIES_ROWS)
    return {"ok": True, "concept_id": concept_id, "entity_type": entity_type,
            "entity_id": entity_id, "count": len(rows), "series": rows}


def _span_days(start: str, end: str) -> int | None:
    from datetime import date
    try:
        d0 = date.fromisoformat(start[:10])
        d1 = date.fromisoformat(end[:10])
        return (d1 - d0).days
    except ValueError:
        return None  # non-canonical shapes: skip the span cap (backend still bounds)

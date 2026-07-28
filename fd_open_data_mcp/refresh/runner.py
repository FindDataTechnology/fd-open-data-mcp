"""Refresh runner: ranked-failover dispatch + fetch logging + ranking feedback.

A scheduled refresh uses the same dispatch path as on-demand reads, so
failover and ranking feedback (``record_fetch_outcome`` -> accessibility
self-tuning) are automatic (spec scheduled-refresh).
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from fd_open_data_mcp.fetch.dispatch import dispatch_one
from fd_open_data_mcp.models import Execution, Schedule, SemanticObservation


def refresh_concept(
    session: Session, concept_id: int, entity_type: str, entity_id: int, date: str,
) -> dict:
    """Force a refresh for one (concept, entity, date). Records an execution row."""
    started = datetime.now(timezone.utc)
    status = "failed"
    detail = ""
    try:
        r = dispatch_one(session, concept_id, entity_type, entity_id, date, requested_date=date)
        if r is not None and r.get("value") is not None:
            status = "success"
            detail = f"via {r.get('source_used')}"
        else:
            status = "failed"
            detail = "no source succeeded"
    except Exception as e:  # noqa: BLE001
        status = "failed"
        detail = str(e)
    session.add(Execution(
        concept_id=concept_id, status=status, started_at=started,
        finished_at=datetime.now(timezone.utc), detail=detail,
    ))
    session.commit()
    return {"concept_id": concept_id, "status": status, "detail": detail}


def run_schedule(session: Session, schedule_id: int) -> dict:
    """Run a schedule: refresh its concept for the most recent cached
    observation of each entity that has one.

    v1 refreshes the latest cached (entity, date) per concept; a full impl
    would iterate all subscribed entities + a date window.
    """
    sched = session.get(Schedule, schedule_id)
    if sched is None:
        return {"schedule_id": schedule_id, "status": "not_found"}
    sched.last_run_at = datetime.now(timezone.utc)
    session.commit()

    obs = (
        session.query(SemanticObservation)
        .filter_by(concept_id=sched.concept_id)
        .order_by(SemanticObservation.date.desc())
        .first()
    )
    if obs is None:
        return {"schedule_id": schedule_id, "concept_id": sched.concept_id,
                "status": "no_cached_observation"}
    result = refresh_concept(session, sched.concept_id, obs.entity_type, obs.entity_id, obs.date)
    return {"schedule_id": schedule_id, "concept_id": sched.concept_id, **result}

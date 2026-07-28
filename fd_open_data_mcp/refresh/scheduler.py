"""Frequency-driven refresh schedule generator.

Each concept's ``indicator_defs.frequency`` maps to a cron expression; a
``schedules`` row is created (idempotent - re-running updates the expr).
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from fd_open_data_mcp.models import Concept, Schedule

# frequency -> cron expr (UTC). Off-round-minute cadences per scheduler convention.
_FREQ_CRON: dict[str, str] = {
    "realtime": "*/5 * * * *",
    "daily": "17 8 * * 1-5",       # weekday after CN market open
    "weekly": "13 0 * * 1",
    "monthly": "11 0 1 * *",
    "quarterly": "11 0 1 1,4,7,10 *",
    "yearly": "11 0 1 1 *",
    "irregular": "11 0 * * 1",
}


def generate_schedules(session: Session, concept_id: int | None = None) -> dict:
    """Create/update schedules for concepts with a known frequency."""
    q = session.query(Concept)
    if concept_id is not None:
        q = q.filter_by(id=concept_id)
    concepts = q.all()
    created = 0
    updated = 0
    for c in concepts:
        cron = _FREQ_CRON.get((c.frequency or "").lower())
        if cron is None:
            continue
        existing = session.query(Schedule).filter_by(concept_id=c.id).first()
        if existing is not None:
            existing.cron_expr = cron
            updated += 1
        else:
            session.add(Schedule(concept_id=c.id, cron_expr=cron, timezone="UTC", enabled=True))
            created += 1
    session.commit()
    return {"created": created, "updated": updated, "total_concepts": len(concepts)}


def list_schedules(session: Session) -> list[dict]:
    return [s.toDict() for s in session.query(Schedule).all()]

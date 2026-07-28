"""Read-through concept-keyed cache (semantic_observations).

Staleness TTL is derived from the concept's frequency. Conflict policy: keep
the highest-ranked source's value with ``source_used``; never merge values
from different sources (design.md D8, D9; spec concept-fetch).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy.orm import Session

from fd_open_data_mcp.models import Concept, SemanticObservation

# TTL by frequency: how long a cached observation is considered fresh.
_TTL: dict[str, timedelta] = {
    "realtime": timedelta(minutes=15),
    "daily": timedelta(hours=20),
    "irregular": timedelta(days=1),
    "weekly": timedelta(days=6),
    "monthly": timedelta(days=25),
    "quarterly": timedelta(days=80),
    "yearly": timedelta(days=300),
    "unknown": timedelta(hours=1),
}


def ttl_for(frequency: Optional[str]) -> timedelta:
    return _TTL.get((frequency or "").lower(), timedelta(hours=1))


def is_stale(obs: SemanticObservation, frequency: Optional[str]) -> bool:
    if obs.fetched_at is None:
        return True
    # SQLite stores datetimes naive (no tzinfo); coerce to UTC for comparison.
    fetched = obs.fetched_at
    if fetched.tzinfo is None:
        fetched = fetched.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - fetched > ttl_for(frequency)


def read_cache(
    session: Session, concept_id: int, entity_type: str, entity_id: int, date: str,
) -> Optional[SemanticObservation]:
    return session.query(SemanticObservation).filter_by(
        concept_id=concept_id, entity_type=entity_type, entity_id=entity_id, date=date,
    ).first()


def write_cache(
    session: Session, concept_id: int, entity_type: str, entity_id: int,
    date: str, value: Optional[str], unit: Optional[str], source_used: str,
) -> SemanticObservation:
    """Upsert one observation.

    The caller (dispatch) selects the source by rank; we store a single row per
    (concept, entity, date) with ``source_used`` attached. Re-fetch overwrites
    the value and bumps ``fetched_at``. Values from different sources are never
    merged into one row.
    """
    obs = read_cache(session, concept_id, entity_type, entity_id, date)
    now = datetime.now(timezone.utc)
    if obs is None:
        obs = SemanticObservation(
            concept_id=concept_id, entity_type=entity_type, entity_id=entity_id,
            date=date, value=value, unit=unit, source_used=source_used, fetched_at=now,
        )
        session.add(obs)
    else:
        obs.value = value
        obs.unit = unit
        obs.source_used = source_used
        obs.fetched_at = now
    session.commit()
    return obs

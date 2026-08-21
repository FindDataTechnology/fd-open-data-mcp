"""Read-through concept-keyed cache (semantic_observations).

Staleness TTL is derived from the concept's frequency. Conflict policy: keep
the highest-ranked source's value with ``source_used``; never merge values
from different sources (design.md D8, D9; spec concept-fetch).

Sharded reads: on the Postgres coordinator (guangzhou-xinru) the dedup view
``semantic_observations_read`` unions the local base table with the FDW-backed
shards (e.g. astock_daily on xinru3), preferring a fresh local row over a stale
shard row per (concept, entity, date, granularity) key. The READ path
(``read_cache`` / ``read_cache_range``) consults that view when present so
shard rows are visible to dispatch; the WRITE path keeps targeting the base
table (the view is read-only), so upserts are unaffected. On SQLite / a local
DB without the view, reads transparently fall back to the base table.

Historical immutability: an observation whose period has fully elapsed
(yesterday's close, last month's CPI, last year's GDP) is a final fact — its
value can no longer change, so ``is_stale`` returns False for it regardless of
``fetched_at``. Only the *current* period (today / this month / this year),
whose value may still be revised, is subject to the fetched_at TTL. This stops
dispatch from re-fetching tens of millions of immutable historical rows on
every read.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional, Union

from sqlalchemy import text
from sqlalchemy.orm import Session

from fd_open_data_mcp.models import SemanticObservation

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


def _today() -> str:
    """Today's canonical date (UTC, 'YYYY-MM-DD') — the immutability cut-off."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _period_final(obs_date: Optional[str], granularity: Optional[str], today_str: str) -> bool:
    """Has the observation's period fully elapsed?

    A past day/month/year is an immutable historical fact (its value is final);
    the current period may still be revised and is therefore TTL-gated. Be
    conservative: an unknown/empty date is never treated as final.
    """
    g = (granularity or "day").lower()
    if not obs_date or len(obs_date) < 4:
        return False
    if g in ("month", "monthly"):
        return obs_date[:7] < today_str[:7]      # a prior month
    if g in ("year", "yearly"):
        return obs_date[:4] < today_str[:4]       # a prior year
    return obs_date < today_str                    # day (default) + any other


def is_stale(obs, frequency: Optional[str]) -> bool:
    if obs.fetched_at is None:
        return True
    # A fully-elapsed period is an immutable fact — never stale, even if
    # fetched_at is far older than the TTL.
    if _period_final(obs.date, getattr(obs, "granularity", None), _today()):
        return False
    # SQLite stores datetimes naive (no tzinfo); coerce to UTC for comparison.
    fetched = obs.fetched_at
    if fetched.tzinfo is None:
        fetched = fetched.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - fetched > ttl_for(frequency)


# --- read relation: dedup view (PG coordinator) or base table (fallback) -----

# Memoized per engine: does the coordinator expose semantic_observations_read?
# Keyed by id(engine); safe because prod keeps one PG engine for the process
# lifetime and the SQLite test path short-circuits without probing.
_view_available: dict[int, bool] = {}

# Columns selected from the view, in a stable order for _ReadRow. A static
# literal (no user input) so interpolating it into the statement is safe.
_READ_COLS = (
    "id, concept_id, entity_type, entity_id, date, granularity, "
    "value, unit, source_used, fetched_at"
)

# A cached read may return a mapped SemanticObservation (base-table fallback)
# or a _ReadRow (view path); dispatch only reads attributes off either.
CachedObs = Union[SemanticObservation, "_ReadRow"]


class _ReadRow:
    """Lightweight attribute bag for a row read from the dedup view.

    Not an ORM-mapped class (the view is read-only and exists only on the PG
    coordinator), so it never participates in ``Base.metadata.create_all`` and
    can never be accidentally written. Dispatch only reads attributes off it,
    which matches how it reads ``SemanticObservation``.
    """

    __slots__ = (
        "id", "concept_id", "entity_type", "entity_id", "date", "granularity",
        "value", "unit", "source_used", "fetched_at",
    )

    def __init__(self, row) -> None:
        m = dict(row._mapping)
        for k in self.__slots__:
            setattr(self, k, m.get(k))


def _use_view(session: Session) -> bool:
    eng = session.get_bind()
    key = id(eng)
    cached = _view_available.get(key)
    if cached is not None:
        return cached
    # SQLite (unit tests, local dev) never has the view — skip the probe.
    if eng.dialect.name == "sqlite":
        _view_available[key] = False
        return False
    try:
        # to_regclass is a cheap catalog lookup; it does NOT execute the
        # view. A bare SELECT ... LIMIT 1 on the UNION ALL view can
        # materialize both arms + the row_number window (the 5GB spill
        # that once filled the GZ disk), so we must not probe that way.
        oid = session.execute(
            text("SELECT to_regclass('semantic_observations_read')")
        ).scalar()
    except Exception:  # noqa: BLE101 — transient (e.g. a tunnel reset) -> base fallback
        # Do NOT memoize: a momentary connection reset at startup would
        # otherwise permanently disable view reads for the process lifetime.
        # Fall back to the base table for THIS read and re-probe next time.
        return False
    available = bool(oid)
    _view_available[key] = available
    return available


def read_cache(
    session: Session, concept_id: int, entity_type: str, entity_id: int, date: str,
) -> Optional[CachedObs]:
    """Read one cached observation for the dispatch (read) path.

    Prefers the dedup view (so FDW-backed shard rows are visible); falls back
    to the base table on SQLite / a DB without the view.
    """
    if _use_view(session):
        row = session.execute(text(
            f"SELECT {_READ_COLS} FROM semantic_observations_read "
            "WHERE concept_id=:c AND entity_type=:t AND entity_id=:e AND date=:d "
            "LIMIT 1"
        ), {"c": concept_id, "t": entity_type, "e": entity_id, "d": date}).first()
        return _ReadRow(row) if row else None
    return _read_base(session, concept_id, entity_type, entity_id, date)


def read_cache_range(
    session: Session, concept_id: int, entity_type: str, entity_id: int,
    start: str, end: str,
) -> list:
    """All cached observations in ``[start, end]`` (inclusive), date-ordered.

    Dates are stored as 'YYYY-MM-DD' strings, so lexicographic bounds work.
    Prefers the dedup view; falls back to the base table.
    """
    if _use_view(session):
        result = session.execute(text(
            f"SELECT {_READ_COLS} FROM semantic_observations_read "
            "WHERE concept_id=:c AND entity_type=:t AND entity_id=:e "
            "AND date>=:s AND date<=:e2 ORDER BY date"
        ), {"c": concept_id, "t": entity_type, "e": entity_id,
            "s": start, "e2": end})
        return [_ReadRow(r) for r in result]
    return _read_base_range(session, concept_id, entity_type, entity_id, start, end)


def _read_base(
    session: Session, concept_id: int, entity_type: str, entity_id: int, date: str,
) -> Optional[SemanticObservation]:
    """Base-table lookup — used by the WRITE path to find the upsert target.

    The view is read-only, so writes must resolve against the base table.
    """
    return session.query(SemanticObservation).filter_by(
        concept_id=concept_id, entity_type=entity_type, entity_id=entity_id, date=date,
    ).first()


def _read_base_range(
    session: Session, concept_id: int, entity_type: str, entity_id: int,
    start: str, end: str,
) -> list[SemanticObservation]:
    return (
        session.query(SemanticObservation)
        .filter(
            SemanticObservation.concept_id == concept_id,
            SemanticObservation.entity_type == entity_type,
            SemanticObservation.entity_id == entity_id,
            SemanticObservation.date >= start,
            SemanticObservation.date <= end,
        )
        .order_by(SemanticObservation.date)
        .all()
    )


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
    obs = _read_base(session, concept_id, entity_type, entity_id, date)
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


def write_cache_range(
    session: Session, concept_id: int, entity_type: str, entity_id: int,
    rows: dict[str, object], unit: Optional[str], source_used: str,
) -> int:
    """Bulk-upsert observations for ``{date: value}`` in one commit.

    Same conflict policy as ``write_cache`` (one row per key, re-fetch
    overwrites); this is the batch form used by ``read_range`` so a range
    fetch costs one commit instead of one per date. Returns rows written.
    """
    if not rows:
        return 0
    existing = {
        obs.date: obs
        for obs in _read_base_range(
            session, concept_id, entity_type, entity_id, min(rows), max(rows))
    }
    now = datetime.now(timezone.utc)
    for d, value in rows.items():
        obs = existing.get(d)
        if obs is None:
            session.add(SemanticObservation(
                concept_id=concept_id, entity_type=entity_type, entity_id=entity_id,
                date=d, value=str(value), unit=unit, source_used=source_used,
                fetched_at=now,
            ))
        else:
            obs.value = str(value)
            obs.unit = unit
            obs.source_used = source_used
            obs.fetched_at = now
    session.commit()
    return len(rows)

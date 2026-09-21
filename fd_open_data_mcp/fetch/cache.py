"""Read-through concept-keyed cache (semantic_observations).

Staleness TTL is derived from the concept's frequency. Conflict policy
(add-multi-source-observations): one row per (point, source) — values from
different sources COEXIST, never merged or overwritten; a plain read returns
the highest-ranked source's row (source_rankings at query time, so ranking
churn takes effect without rewriting rows).

Sharded reads: on the Postgres coordinator (guangzhou-xinru) the dedup view
``semantic_observations_read`` unions the local base table with the FDW-backed
shards (e.g. astock_daily on xinru3), preferring a fresh local row over a stale
shard row per (concept, entity, date, granularity) key. The READ path
(``read_cache`` / ``read_cache_range``) consults that view when present so
shard rows are visible to dispatch; the WRITE path keeps targeting the base
table (the view is read-only), so upserts are unaffected. On SQLite / a local
DB without the view, reads transparently fall back to the base table. The view
collapses sources per point (its dedup is not yet source-aware — see the
007 ops note in docs/migrations-archive/), so view-path reads return the
view's chosen row;
source-specific reads and ``all_sources`` query the base table directly.

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

from sqlalchemy import or_, text
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


def period_prefixes(date: str, frequency: Optional[str]) -> Optional[list[str]]:
    """LIKE prefixes covering the period ``date`` falls in, or None for exact match.

    Stored observation dates are not normalized: a yearly row may be the bare
    ``"2021"`` while a monthly row is ``"2005-02-01"``. Matching by period
    prefix (``2021%``, ``2005-02%``) is therefore robust to both conventions,
    where exact-date equality silently missed every off-anchor request.

    Returns None for daily/weekly/irregular/unknown frequencies — those keep
    exact-date semantics (their period IS the date).
    """
    f = (frequency or "").lower()
    if len(date) < 4:
        return None
    if f in ("yearly", "annual", "year"):
        return [date[:4]]
    if len(date) < 7:
        return None
    if f in ("monthly", "month"):
        return [date[:7]]
    if f in ("quarterly", "quarter"):
        year, month = int(date[:4]), int(date[5:7])
        first = ((month - 1) // 3) * 3 + 1
        return [f"{year:04d}-{first + i:02d}" for i in range(3)]
    return None


def _pick_period_date(rows, date: str) -> Optional[str]:
    """The stored date to serve for a request inside its period.

    Prefer the row closest at-or-before the requested date (the value as known
    at that point in the period); if every row lies after it, the earliest.
    """
    if not rows:
        return None
    dates = sorted({r.date for r in rows})
    before = [d for d in dates if d <= date]
    return before[-1] if before else dates[0]


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


def _source_order(session: Session, concept_id: int) -> dict[str, int]:
    """source -> preference position for a concept (lower = better), from
    source_rankings (quality desc, then accessibility desc; unranked sources
    sort after ranked ones, alphabetically for determinism). Computed per call:
    a ranking change takes effect on the next read with no stored-row rewrite."""
    from fd_open_data_mcp.models import SourceRanking

    rows = (
        session.query(SourceRanking.source, SourceRanking.quality, SourceRanking.accessibility)
        .filter(SourceRanking.concept_id == concept_id)
        .all()
    )
    ordered = sorted(rows, key=lambda r: (-r.quality, -r.accessibility, r.source))
    return {r.source: i for i, r in enumerate(ordered)}


def _prefer(candidates: list, concept_id: int, order: dict[str, int]):
    """Pick the highest-ranked row among candidates (ties: alphabetical source,
    then row id). candidates share one observation point."""
    if not candidates:
        return None
    unranked = len(order)

    def _key(o):
        src = o.source_used or ""
        return (order.get(src, unranked), src, o.id or 0)

    return min(candidates, key=_key)


def read_cache(
    session: Session, concept_id: int, entity_type: str, entity_id: int, date: str,
    source: Optional[str] = None, frequency: Optional[str] = None,
) -> Optional[CachedObs]:
    """Read one cached observation for the dispatch (read) path.

    Prefers the dedup view (so FDW-backed shard rows are visible); falls back
    to the base table on SQLite / a DB without the view. ``source`` selects one
    source's row explicitly (base-table query: the view collapses sources).
    Without ``source``, the highest-ranked source's row wins (base path; the
    view path returns the view's pre-deduped row — see module docstring).
    """
    prefixes = period_prefixes(date, frequency)
    if source is not None:
        rows = _read_base_period(session, concept_id, entity_type, entity_id, date,
                                 prefixes, source=source)
        chosen = _pick_period_date(rows, date)
        return next((r for r in rows if r.date == chosen), None) if chosen else None
    if _use_view(session):
        if prefixes:
            clause = " OR ".join(f"date LIKE '{p}%'" for p in prefixes)  # literal prefixes, no user input
            rows = session.execute(text(
                f"SELECT {_READ_COLS} FROM semantic_observations_read "
                f"WHERE concept_id=:c AND entity_type=:t AND entity_id=:e AND ({clause})"
            ), {"c": concept_id, "t": entity_type, "e": entity_id}).all()
            if not rows:
                return None
            wrapped = [_ReadRow(r) for r in rows]
            chosen = _pick_period_date(wrapped, date)
            return next((r for r in wrapped if r.date == chosen), None)
        row = session.execute(text(
            f"SELECT {_READ_COLS} FROM semantic_observations_read "
            "WHERE concept_id=:c AND entity_type=:t AND entity_id=:e AND date=:d "
            "LIMIT 1"
        ), {"c": concept_id, "t": entity_type, "e": entity_id, "d": date}).first()
        return _ReadRow(row) if row else None
    rows = _read_base_period(session, concept_id, entity_type, entity_id, date, prefixes)
    if not rows:
        return None
    if len(rows) == 1:
        return rows[0]
    chosen = _pick_period_date(rows, date)
    same = [r for r in rows if r.date == chosen]
    if len(same) == 1:
        return same[0]
    return _prefer(same, concept_id, _source_order(session, concept_id))


def read_cache_all(
    session: Session, concept_id: int, entity_type: str, entity_id: int, date: str,
) -> list:
    """Every held row for one observation point, best-ranked source first.

    Base-table only: the coordinator dedup view collapses sources per point, so
    comparison mode must bypass it (shard rows are not included — see module
    docstring). Never dispatches; a missing source is a coverage gap, not a
    surprise fetch.
    """
    rows = _read_base_all(session, concept_id, entity_type, entity_id, date)
    if len(rows) > 1:
        order = _source_order(session, concept_id)
        rows = sorted(rows, key=lambda o: (order.get(o.source_used or "", len(order)),
                                           o.source_used or ""))
    return rows


def read_cache_range(
    session: Session, concept_id: int, entity_type: str, entity_id: int,
    start: str, end: str, source: Optional[str] = None,
) -> list:
    """Cached observations in ``[start, end]`` (inclusive), date-ordered.

    Dates are stored as 'YYYY-MM-DD' strings, so lexicographic bounds work.
    Prefers the dedup view; falls back to the base table. When several sources
    hold the same (date, granularity) point, the highest-ranked source's row is
    returned — one row per point, as plain reads expect. ``source`` narrows to
    one source's rows (base-table query).
    """
    if source is not None or not _use_view(session):
        rows = _read_base_range(session, concept_id, entity_type, entity_id, start, end,
                                source=source)
        return _dedupe_range(rows, concept_id, session)
    result = session.execute(text(
        f"SELECT {_READ_COLS} FROM semantic_observations_read "
        "WHERE concept_id=:c AND entity_type=:t AND entity_id=:e "
        "AND date>=:s AND date<=:e2 ORDER BY date"
    ), {"c": concept_id, "t": entity_type, "e": entity_id,
        "s": start, "e2": end})
    return [_ReadRow(r) for r in result]


def _dedupe_range(rows: list, concept_id: int, session: Session) -> list:
    """Collapse multi-source rows per (date, granularity) to the preferred one."""
    if len(rows) <= 1:
        return rows
    order = _source_order(session, concept_id)
    best: dict[tuple, object] = {}
    for r in rows:
        key = (r.date, getattr(r, "granularity", None) or "day")
        cur = best.get(key)
        if cur is None or _prefer([r, cur], concept_id, order) is r:
            best[key] = r
    return sorted(best.values(), key=lambda r: r.date)


def _read_base(
    session: Session, concept_id: int, entity_type: str, entity_id: int, date: str,
    source: Optional[str] = None,
) -> Optional[SemanticObservation]:
    """Base-table lookup — used by the WRITE path to find the upsert target.

    The view is read-only, so writes must resolve against the base table.
    Upserts are per (point, source): ``source`` scopes the target so this
    source's write never lands on — or overwrites — another source's row.
    """
    q = session.query(SemanticObservation).filter_by(
        concept_id=concept_id, entity_type=entity_type, entity_id=entity_id, date=date,
    )
    if source is not None:
        q = q.filter(SemanticObservation.source_used == source)
    return q.first()


def _read_base_all(
    session: Session, concept_id: int, entity_type: str, entity_id: int, date: str,
) -> list[SemanticObservation]:
    return session.query(SemanticObservation).filter_by(
        concept_id=concept_id, entity_type=entity_type, entity_id=entity_id, date=date,
    ).all()


def _read_base_period(
    session: Session, concept_id: int, entity_type: str, entity_id: int, date: str,
    prefixes: Optional[list[str]] = None, source: Optional[str] = None,
) -> list[SemanticObservation]:
    """Base-table lookup by period prefix (exact date when ``prefixes`` is None)."""
    q = session.query(SemanticObservation).filter_by(
        concept_id=concept_id, entity_type=entity_type, entity_id=entity_id,
    )
    if prefixes:
        q = q.filter(or_(*[SemanticObservation.date.like(f"{p}%") for p in prefixes]))
    else:
        q = q.filter(SemanticObservation.date == date)
    if source is not None:
        q = q.filter(SemanticObservation.source_used == source)
    return q.all()


def _read_base_range(
    session: Session, concept_id: int, entity_type: str, entity_id: int,
    start: str, end: str, source: Optional[str] = None,
) -> list[SemanticObservation]:
    q = (
        session.query(SemanticObservation)
        .filter(
            SemanticObservation.concept_id == concept_id,
            SemanticObservation.entity_type == entity_type,
            SemanticObservation.entity_id == entity_id,
            SemanticObservation.date >= start,
            SemanticObservation.date <= end,
        )
    )
    if source is not None:
        q = q.filter(SemanticObservation.source_used == source)
    return q.order_by(SemanticObservation.date).all()


def write_cache(
    session: Session, concept_id: int, entity_type: str, entity_id: int,
    date: str, value: Optional[str], unit: Optional[str], source_used: str,
) -> SemanticObservation:
    """Upsert one observation for ``source_used``.

    One row per (point, source): the write targets this source's own row —
    an existing row from ANOTHER source is left untouched and a new row is
    added for this source instead. Re-fetch overwrites this row's value and
    bumps ``fetched_at``. Values from different sources are never merged.
    """
    obs = _read_base(session, concept_id, entity_type, entity_id, date, source=source_used)
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
        obs.fetched_at = now
    session.commit()
    return obs


def write_cache_range(
    session: Session, concept_id: int, entity_type: str, entity_id: int,
    rows: dict[str, object], unit: Optional[str], source_used: str,
) -> int:
    """Bulk-upsert observations for ``{date: value}`` in one commit.

    Same per-source conflict policy as ``write_cache`` (one row per (date,
    source); another source's rows are untouched); this is the batch form used
    by ``read_range`` so a range fetch costs one commit instead of one per
    date. Returns rows written.
    """
    if not rows:
        return 0
    existing = {
        obs.date: obs
        for obs in _read_base_range(
            session, concept_id, entity_type, entity_id, min(rows), max(rows),
            source=source_used,
        )
        if obs.source_used == source_used
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
            obs.fetched_at = now
    session.commit()
    return len(rows)

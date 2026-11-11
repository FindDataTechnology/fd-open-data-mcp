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

import logging
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional, Union
from uuid import uuid4

from sqlalchemy import or_, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from fd_open_data_mcp.entities.resolver import check_entity_identity
from fd_open_data_mcp.errors import FinalizedObservationError
from fd_open_data_mcp.fetch.finalization import is_finalized, resolve_window

from fd_open_data_mcp.models import Concept as _Concept
from fd_open_data_mcp.models import ObservationRevision, SemanticObservation

logger = logging.getLogger(__name__)
from fd_open_data_mcp.semantic.dates import (
    SHAPE_LIKE_PATTERNS,
    obs_month_of,
    observation_period,
    sql_bounds,
    window_span,
)
from fd_open_data_mcp.visibility.coverage import bump_rollups

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
    """Has the observation's period left its revision window?

    Delegates to :mod:`fd_open_data_mcp.fetch.finalization`, which owns the
    window rules. The important difference from the pre-window rule: a **year**
    is not final on 1 January of the next year — annual statistics get revised
    for a year or two, so yearly values stay revisable through the whole
    following year and finalize on 1 January of the year after.

    A past day/month is still an immutable historical fact; only the current
    period is revisable. Be conservative: an unknown/empty date is never
    treated as final.
    """
    if not obs_date or len(str(obs_date)) < 4:
        return False
    try:
        now = datetime.strptime(today_str[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return False
    return is_finalized(obs_date, granularity, now=now)


def is_finalized_obs(obs, concept=None) -> bool:
    """Is this stored observation past its revision window (immutable)?"""
    window = resolve_window(getattr(obs, "granularity", None) or "day", concept)
    return is_finalized(getattr(obs, "date", None),
                        getattr(obs, "granularity", None), window)


def should_refetch(obs, frequency: Optional[str], concept=None) -> bool:
    """Does this stored row need another upstream attempt?

    Deliberately distinct from "is it finalized": finality answers *may this
    value still change*, refetching answers *should we go ask again*. Keeping
    them apart is what stops a window change from silently becoming an upstream
    traffic change — a finalized row is never refetched no matter how old
    ``fetched_at`` is.
    """
    if getattr(obs, "fetched_at", None) is None:
        return True
    if is_finalized_obs(obs, concept):
        return False
    fetched = obs.fetched_at
    # SQLite stores datetimes naive (no tzinfo); coerce to UTC for comparison.
    if fetched.tzinfo is None:
        fetched = fetched.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - fetched > ttl_for(frequency)


def is_stale(obs, frequency: Optional[str]) -> bool:
    """Backwards-compatible alias: a row is "stale" exactly when it needs refetching."""
    return should_refetch(obs, frequency)


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
    check_entity_identity(session, entity_type, entity_id)
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
    check_entity_identity(session, entity_type, entity_id)
    rows = _read_base_all(session, concept_id, entity_type, entity_id, date)
    if len(rows) > 1:
        order = _source_order(session, concept_id)
        rows = sorted(rows, key=lambda o: (order.get(o.source_used or "", len(order)),
                                           o.source_used or ""))
    return rows


def read_cache_range(
    session: Session, concept_id: int, entity_type: str, entity_id: int,
    start: str, end: str, source: Optional[str] = None,
    excluded: Optional[list[tuple[str, str]]] = None,
) -> list:
    """Cached observations in ``[start, end]`` (inclusive), date-ordered.

    Window bounds are shape-aware, not plain string comparisons: the ``date``
    column is text and carries the six shapes documented in
    ``semantic/dates.py``, under which ``"2023" < "2023-01-01"`` (a bare year
    sorts before every day inside it) and ``"2026-W40" > "2026-12-31"``
    (``'W' > '1'``). A lexicographic ``date >= start AND date <= end``
    therefore dropped every bare-year and ISO-week row from the window — whole
    years missing, no gap, no note. The SQL predicate is widened to whole
    years (``sql_bounds``) and this function keeps only the rows whose parsed
    period (``observation_period``) actually intersects the window.

    Period intersection — not period-start containment — matches the point
    reader (``period_prefixes``): a stored ``"2023"`` is the annual value for
    every date in 2023, so a window inside 2023 holds it.

    ``excluded``: optional out-parameter. When passed, it receives
    ``(date, reason)`` for every stored date of this (concept, entity) that
    cannot be placed on the calendar — in-window garbage as well as shapes the
    widened predicate cannot even reach (the ``LATEST`` first-crawl sentinel
    sorts above every ``YYYY-…`` string). Those rows are never silently put in
    a window; a caller that asks gets them reported instead of dropped.

    Prefers the dedup view; falls back to the base table. When several sources
    hold the same (date, granularity) point, the highest-ranked source's row is
    returned — one row per point, as plain reads expect. ``source`` narrows to
    one source's rows (base-table query).
    """
    check_entity_identity(session, entity_type, entity_id)
    lo, hi = sql_bounds(start, end)
    if source is not None or not _use_view(session):
        rows = _read_base_range(session, concept_id, entity_type, entity_id, lo, hi,
                                source=source)
        rows = _dedupe_range(rows, concept_id, session)
    else:
        result = session.execute(text(
            f"SELECT {_READ_COLS} FROM semantic_observations_read "
            "WHERE concept_id=:c AND entity_type=:t AND entity_id=:e "
            "AND date>=:s AND date<=:e2 ORDER BY date"
        ), {"c": concept_id, "t": entity_type, "e": entity_id,
            "s": lo, "e2": hi})
        rows = [_ReadRow(r) for r in result]
    if excluded is not None:
        excluded.extend(_unmappable_dates(session, concept_id, entity_type, entity_id,
                                          source=source))
    return _window_only(rows, start, end, excluded)


def _window_only(
    rows: list, start: str, end: str,
    excluded: Optional[list[tuple[str, str]]] = None,
) -> list:
    """Keep the rows whose parsed period intersects ``[start, end]``.

    A row that fits no shape is dropped from the window (it has no position)
    and, when the caller asked for a report, appended to ``excluded`` with its
    reason instead of vanishing. An unparseable *window* keeps the previous
    lexicographic meaning — the rows already satisfy it.
    """
    span = window_span(start, end)
    if span is None:
        return list(rows)
    decorated = []
    for r in rows:
        period = observation_period(r.date)
        if period is None:
            if excluded is not None:
                excluded.append((r.date, "unparseable_date"))
            continue
        if period[1] >= span[0] and period[0] <= span[1]:
            decorated.append((period[0], r.date, r))
    decorated.sort(key=lambda item: (item[0], item[1]))
    return [r for _, _, r in decorated]


def _unmappable_dates(
    session: Session, concept_id: int, entity_type: str, entity_id: int,
    source: Optional[str] = None,
) -> list[tuple[str, str]]:
    """Stored dates for one point that fit none of ``semantic/dates.py``'s shapes.

    Found by shape, not by position: the ``LATEST`` sentinel sorts above every
    ``YYYY-…`` string, so no widened window can reach it, yet a caller must be
    able to tell "window empty" from "this point's date is not a date". Only
    runs when a caller asks for the report (``read_cache_range(excluded=...)``),
    so write-path lookups never pay for it.
    """
    mappable = or_(*[SemanticObservation.date.like(p) for p in SHAPE_LIKE_PATTERNS])
    q = session.query(SemanticObservation.date).filter(
        SemanticObservation.concept_id == concept_id,
        SemanticObservation.entity_type == entity_type,
        SemanticObservation.entity_id == entity_id,
        ~mappable,
    )
    if source is not None:
        q = q.filter(SemanticObservation.source_used == source)
    return [(d, "unparseable_date") for d in sorted({row[0] for row in q.all()}) if d]


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
    source: Optional[str] = None, granularity: Optional[str] = None,
) -> Optional[SemanticObservation]:
    """Base-table lookup — used by the WRITE path to find the upsert target.

    The view is read-only, so writes must resolve against the base table.
    Upserts are per (point, source): ``source`` scopes the target so this
    source's write never lands on — or overwrites — another source's row.
    """
    q = session.query(SemanticObservation).filter_by(
        concept_id=concept_id, entity_type=entity_type, entity_id=entity_id, date=date,
    )
    # granularity scopes the upsert target (write path): a day row and a
    # month row of the same date are DISTINCT points (uq_sem_obs). Reads
    # leave it None to match across cadences.
    if granularity is not None:
        q = q.filter(SemanticObservation.granularity == granularity)
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
    granularity: Optional[str] = None,
) -> list[SemanticObservation]:
    """Base-table rows whose ``date`` satisfies the given bounds as STRINGS.

    The comparison here is lexicographic, so it is only correct for the bounds
    the caller supplies. The window reader passes ``dates.sql_bounds(...)``
    (whole-year bounds) and then decides each row on its parsed period; the
    write paths pass canonical ``min(rows)``/``max(rows)`` keys, which are
    exact by construction.
    """
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
    if granularity is not None:
        q = q.filter(SemanticObservation.granularity == granularity)
    if source is not None:
        q = q.filter(SemanticObservation.source_used == source)
    return q.order_by(SemanticObservation.date).all()


def _enforce_finalized_write_guard(
    session: Session, concept_id: int, entity_type: str, entity_id: int,
    granularity: str, existing: Iterable, *, correction: bool,
    reason: Optional[str],
) -> None:
    """定型写闸：拒绝改写已定型的**已有**观测。

    边界是「改写」，不是「写入」——首次写入一个历史点是补数（gap fill / 冷启动），
    不是修订。把首次写入也挡掉，历史区间将永远补不了数。

    因此：只检查命中的既有行；没有任何既有行时放行。

    确需更正时用法是 ``correction=True`` + 非空 ``reason``：放行，并照常落
    ``observation_revisions`` 留痕（调用点已负责写修订行）。空 reason 一律拒绝——
    没有理由的更正与静默覆写无法区分。

    概念级的窗口声明非法时 `resolve_window` 抛错，这里**不吞**：静默退化为默认会让
    "声明了却没生效"变成一个看不见的坑。
    """
    if correction and not (reason or "").strip():
        raise FinalizedObservationError(
            "correction=True 需要非空 reason：没有理由的更正与静默覆写无法区分",
        )

    blocked = []
    hit_window: dict = {}
    concept = session.get(_Concept, concept_id) if session is not None else None
    window = resolve_window(granularity, concept)
    for obs in existing:
        if is_finalized(getattr(obs, "date", None), granularity, window):
            blocked.append((concept_id, entity_type, entity_id,
                            obs.date, granularity))
            hit_window = {"grace_days": window.grace_days,
                          "granularity": granularity}

    if not blocked or correction:
        return

    preview = ", ".join(
        f"{c[1]}/{c[2]}/{c[3]}/{c[4]}" for c in blocked[:5]
    )
    more = "" if len(blocked) <= 5 else f" 等共 {len(blocked)} 处"
    raise FinalizedObservationError(
        f"拒绝改写已定型观测：{preview}{more}（窗口 grace_days="
        f"{hit_window.get('grace_days')}/{granularity}）；确需更正请走 correction 通道并给出 reason",
        coordinates=blocked,
        window=hit_window,
    )


def write_cache(
    session: Session, concept_id: int, entity_type: str, entity_id: int,
    date: str, value: Optional[str], unit: Optional[str], source_used: str,
    granularity: str = "day",
    *, correction: bool = False, reason: Optional[str] = None,
) -> SemanticObservation:
    """Upsert one observation for ``source_used``.

    One row per (point, source): the write targets this source's own row —
    an existing row from ANOTHER source is left untouched and a new row is
    added for this source instead. Re-fetch overwrites this row's value and
    bumps ``fetched_at``. Values from different sources are never merged.

    Rewriting an **already finalized** row is refused unless the caller declares
    ``correction=True`` with a ``reason`` (see `_enforce_finalized_write_guard`).
    Creating a previously absent historical point is always allowed.
    """
    obs = _read_base(session, concept_id, entity_type, entity_id, date,
                     source=source_used, granularity=granularity)
    now = datetime.now(timezone.utc)
    if obs is None:
        obs = SemanticObservation(
            concept_id=concept_id, entity_type=entity_type, entity_id=entity_id,
            date=date, granularity=granularity, value=value, unit=unit,
            source_used=source_used, fetched_at=now,
            obs_month=obs_month_of(date),
        )
        session.add(obs)
        inserted, touched = [date], [date]
    else:
        _enforce_finalized_write_guard(
            session, concept_id, entity_type, entity_id, granularity, [obs],
            correction=correction, reason=reason,
        )
        # 每一次覆写都必须留痕，不只是 correction。此前 `write_cache` 从不写修订行，
        # 于是单点刷新（`read` 的活路径）覆盖掉的旧值永久消失——as-of 回放对这类点
        # 只能判 `vintage_unavailable`。这是 `define-ingest-revision-and-coverage` R1
        # 要求「定型前修订 MUST 留痕」在单点写路径上的缺口，本 change 一并补上。
        session.add(ObservationRevision(
            concept_id=concept_id, entity_type=entity_type, entity_id=entity_id,
            date=date, granularity=granularity, source_used=source_used,
            revision_kind="overwrite", old_value=obs.value, new_value=value,
            reason=reason, revised_at=now,
        ))
        obs.value = value
        obs.unit = unit
        obs.fetched_at = now
        obs.obs_month = obs_month_of(date)
        inserted, touched = [], [date]
    bump_rollups(session, concept_id, entity_type, entity_id, source_used,
                 inserted, touched, now, granularity=granularity)
    # 单点写入同样要执行 T 语义：经 fetch(点) 写入的快照也会累积，
    # 只在 range 路径合并会留一条后门（实测 tenure_days 就是逐点写出来的）。
    if _retention_snapshot_class(session, concept_id, source_used) == "T":
        _snapshot_consolidate(session, concept_id, entity_type, entity_id,
                              granularity, source_used, date, reason)
    session.commit()
    return obs


def write_cache_range_new_only(
    session: Session, concept_id: int, entity_type: str, entity_id: int,
    rows: dict[str, object], unit: Optional[str], source_used: str,
    granularity: str = "day",
) -> int:
    """Insert-only variant of :func:`write_cache_range` — never overwrites.

    Gap filling must not rewrite history. A stored price is a frozen fact as of
    when it was fetched; overwriting it with today's fetch changes yesterday's
    backtest — and a forward-adjusted series is rewritten wholesale upstream
    after every dividend, so a "refresh" silently rewrites the entire past.
    This variant adds only dates that are absent and leaves every existing
    value untouched.

    Uses IntegrityError handling rather than ``ON CONFLICT DO NOTHING`` so it
    stays portable across SQLite (tests) and Postgres (production), matching
    the precedent in ``auth_tools.ensure_identity``.

    Returns rows actually inserted — 0 when everything was already present,
    which is what makes a second gap fill a no-op.
    """
    if not rows:
        return 0
    existing = {
        obs.date
        for obs in _read_base_range(
            session, concept_id, entity_type, entity_id, min(rows), max(rows),
            source=source_used, granularity=granularity,
        )
    }
    now = datetime.now(timezone.utc)
    inserted, touched = [], []
    for d, value in sorted(rows.items()):
        if d in existing:
            continue
        session.add(SemanticObservation(
            concept_id=concept_id, entity_type=entity_type, entity_id=entity_id,
            date=d, granularity=granularity, value=str(value), unit=unit,
            source_used=source_used,
            fetched_at=now, obs_month=obs_month_of(d),
        ))
        inserted.append(d)
        touched.append(d)
    try:
        session.flush()
    except IntegrityError:
        # A concurrent writer, or the same date under a granularity row we
        # did not see: adopt nothing, roll the batch back and report zero —
        # the rollup hook never sees rows that did not land.
        session.rollback()
        return 0
    bump_rollups(session, concept_id, entity_type, entity_id, source_used,
                 inserted, touched, now, granularity=granularity)
    session.commit()
    return len(inserted)


def _retention_window_days(session: Session, concept_id: int,
                           source_used: str) -> Optional[int]:
    """W 类（滚动窗口）的窗口天数；非 W 或无契约时返回 None。

    与 :func:`_retention_snapshot_class` 同一套解析，只是这里要的是天数。
    """
    from fd_open_data_mcp.fetch.retention import family_of, resolve_contract

    concept = session.get(_Concept, concept_id)
    if concept is None:
        return None
    contract = resolve_contract(session, source_used, family_of(concept.code))
    if contract is None or contract.retention_class != "W":
        return None
    return contract.retention_window_days


def _drop_out_of_window(session: Session, concept_id: int, entity_type: str,
                        entity_id: int, source_used: str,
                        rows: dict[str, object]) -> dict[str, object]:
    """W 类：把窗口外的点挡在写入之前（不是写完再删）。

    为什么拦在入口：实测（2026-11-11）清完超窗行之后，一次全量重抓又把 234 条
    2021 年的点写了回来——因为 W 类的写路径此前不做任何窗口裁剪。写完再删要多一次
    范围删除，而且每写一次就删一次；在入口丢弃则是零额外成本。

    **只处理完整的 ISO 日期**：`2025`（裸年）、`2025-12` 这类日期与窗口下界比较
    没有唯一含义，猜一个方向就是在替上游决定口径——宁可保留并在日志里说明。
    """
    window_days = _retention_window_days(session, concept_id, source_used)
    if not window_days:
        return rows
    cutoff = (datetime.now(timezone.utc).date()
              - timedelta(days=int(window_days))).isoformat()
    kept: dict[str, object] = {}
    dropped: list[str] = []
    for d, v in rows.items():
        key = str(d)
        if (len(key) == 10 and key[4] == "-" and key[7] == "-"
                and key.replace("-", "").isdigit() and key < cutoff):
            dropped.append(key)
            continue
        kept[d] = v
    if dropped:
        logger.warning(
            "留存窗口：丢弃 %d 个早于 %s 的点（concept_id=%s entity=%s/%s source=%s，"
            "最早 %s）——W 类只保留窗口内数据，重抓历史不会把它们写回来",
            len(dropped), cutoff, concept_id, entity_type, entity_id,
            source_used, min(dropped))
    return kept


def _retention_snapshot_class(session: Session, concept_id: int,
                              source_used: str) -> Optional[str]:
    """该 (概念, 源) 的留存类别；读不到契约时返回 None（= 不额外处理）。

    为什么要单独包一层而不是直接查表：``write_cache_range`` 是热路径，每次写都查
    一次契约会多一次往返；``retention.resolve_contract`` 已按 session 记忆，这里
    只是把"取到 concept.code → 族 → 契约"这段拼起来，并让调用点读起来是一句话。
    """
    from fd_open_data_mcp.fetch.retention import family_of, resolve_contract

    concept = session.get(_Concept, concept_id)
    if concept is None:
        return None
    contract = resolve_contract(session, source_used, family_of(concept.code))
    return contract.retention_class if contract is not None else None


def _snapshot_consolidate(    session: Session, concept_id: int, entity_type: str, entity_id: int,
    granularity: str, source_used: str, keep_date: str,
    revision_reason: Optional[str],
) -> int:
    """T 类（只留当期）写完后把旧快照并掉：只留 ``keep_date`` 那一条。

    为什么是"并"而不是"写完再删"：``bump_rollups`` 按行数记账，删行会让
    ``concept_source_stats`` 的计数单向漂高。这里**原地搬**——把旧行删除前记一条
    batch 修订（区间 + 点数 + 原因），行数净变化只有"多出来的那几条"。

    实测背景（2026-11-11）：``tenure_days`` 这类派生快照每天写一条，近 7 天每实体
    47 行；契约写的是"只留当期"，但写路径没有任何东西在执行它——于是清理过后
    几个月又长回来。本函数就是那条缺失的执行。
    """
    from fd_open_data_mcp.models import ObservationRevision as _Rev

    older = (session.query(SemanticObservation)
             .filter(SemanticObservation.concept_id == concept_id,
                     SemanticObservation.entity_type == entity_type,
                     SemanticObservation.entity_id == entity_id,
                     SemanticObservation.granularity == granularity,
                     SemanticObservation.source_used == source_used,
                     SemanticObservation.date < keep_date)
             .all())
    if not older:
        return 0
    dates = sorted(o.date for o in older)
    session.add(_Rev(
        concept_id=concept_id, entity_type=entity_type, entity_id=entity_id,
        date=max(dates), granularity=granularity, source_used=source_used,
        revision_kind="batch",
        batch_id=f"snapshot-consolidate-{uuid4().hex[:12]}",
        range_start=dates[0], range_end=dates[-1], point_count=len(dates),
        reason=revision_reason or "T 类只留当期：合并旧的快照行",
        revised_at=datetime.now(timezone.utc),
    ))
    for o in older:
        session.delete(o)
    return len(older)


def write_cache_range(
    session: Session, concept_id: int, entity_type: str, entity_id: int,
    rows: dict[str, object], unit: Optional[str], source_used: str,
    granularity: str = "day",
    *, revision_reason: Optional[str] = None,
    revision_batch_id: Optional[str] = None,
) -> int:
    """Bulk-upsert observations for ``{date: value}`` in one commit.

    Same per-source conflict policy as ``write_cache`` (one row per (date,
    source); another source's rows are untouched); this is the batch form used
    by ``read_range`` so a range fetch costs one commit instead of one per
    date. Returns rows written.

    This is the *first fetch* path: it may refresh a value (notably today's
    cell, which the TTL governs). **Gap filling must not use it** — use
    :func:`write_cache_range_new_only`, which never overwrites a stored value.

    Revision trail (define-ingest-revision-and-coverage)
    ----------------------------------------------------
    Refreshing a stored value rewrites it, so the old value now lands in
    ``observation_revisions`` before it is replaced: the requirement is not
    "never overwrite" (the current period is *meant* to be revised) but "an
    overwrite must not make the old value disappear".

    Two shapes:
      * default — one revision row per point.
      * ``revision_batch_id`` — the caller knows this is a whole-series rewrite
        (forward-adjusted prices after a dividend), where per-point history
        costs far more than it is worth: one batch row records the range, the
        point count and the reason instead.
    """
    if not rows:
        return 0
    # 留存契约闸门（establish-data-retention-principle R1/R2）。
    # 默认 off（零开销）；warn 只报警；enforce 缺契约即拒写。放在"定型写闸"之前：
    # 契约回答的是"这个源该不该有数据"，比"这一行能不能改"更前置。
    from fd_open_data_mcp.fetch.retention import check_write_allowed

    check_write_allowed(session, concept_id=concept_id, source=source_used)
    # W 类（滚动窗口）在入口裁掉窗口外的点：重抓历史不能把刚清掉的老行写回来
    # （实测 2026-11-11：清完 aum 超窗行，一次全量重抓又写回 234 条 2021 年的点）。
    rows = _drop_out_of_window(session, concept_id, entity_type, entity_id,
                               source_used, rows)
    if not rows:
        return 0
    existing = {
        obs.date: obs
        for obs in _read_base_range(
            session, concept_id, entity_type, entity_id, min(rows), max(rows),
            source=source_used, granularity=granularity,
        )
    }
    # 定型写闸：只拦「改写已有行」，不拦「首次写入历史点」（那是补数）。
    # 带 reason 的写入视为声明式更正——这对整段重写（前复权）尤其必要：那是合法且
    # 预期的历史重写，会被审计成一条 batch 修订；无 reason 的覆写与静默改写无法区分。
    _enforce_finalized_write_guard(
        session, concept_id, entity_type, entity_id, granularity,
        [existing[d] for d in rows if d in existing],
        correction=bool((revision_reason or "").strip()),
        reason=revision_reason,
    )
    now = datetime.now(timezone.utc)
    inserted, touched = [], []
    revised_points = 0
    for d, value in rows.items():
        obs = existing.get(d)
        if obs is None:
            session.add(SemanticObservation(
                concept_id=concept_id, entity_type=entity_type, entity_id=entity_id,
                date=d, granularity=granularity, value=str(value), unit=unit,
                source_used=source_used,
                fetched_at=now, obs_month=obs_month_of(d),
            ))
            inserted.append(d)
        else:
            new_value = str(value)
            if revision_batch_id is None:
                # 单点修订：旧值必须留下，否则"改了什么"无从回答。
                session.add(ObservationRevision(
                    concept_id=concept_id, entity_type=entity_type, entity_id=entity_id,
                    date=d, granularity=granularity, source_used=source_used,
                    revision_kind="overwrite",
                    old_value=obs.value, new_value=new_value,
                    reason=revision_reason, revised_at=now,
                ))
            revised_points += 1
            obs.value = new_value
            obs.unit = unit
            obs.fetched_at = now
            obs.obs_month = obs_month_of(d)
        touched.append(d)
    if revision_batch_id is not None and revised_points:
        # 整段修订只记一行：范围 + 点数 + 原因。逐点旧值不存是有意的取舍。
        session.add(ObservationRevision(
            concept_id=concept_id, entity_type=entity_type, entity_id=entity_id,
            date=min(rows), granularity=granularity, source_used=source_used,
            revision_kind="batch", batch_id=revision_batch_id,
            range_start=min(rows), range_end=max(rows), point_count=revised_points,
            reason=revision_reason, revised_at=now,
        ))
    bump_rollups(session, concept_id, entity_type, entity_id, source_used,
                 inserted, touched, now, granularity=granularity)
    # T 类（只留当期）在这里执行契约：把当天之前的快照并掉，只留最新一条。
    # 放在 bump_rollups 之后、commit 之前——同事务内，失败则整体回滚，不会半途。
    if _retention_snapshot_class(session, concept_id, source_used) == "T":
        _snapshot_consolidate(session, concept_id, entity_type, entity_id,
                              granularity, source_used, max(rows), revision_reason)
    session.commit()
    return len(rows)

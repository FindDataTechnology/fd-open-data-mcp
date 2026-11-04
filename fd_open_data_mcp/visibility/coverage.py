"""Data-coverage aggregation over ``semantic_observations``
(add-panel-crawl-observability D4; cache added by panel-data-coverage-cache).

One read-only point-grain aggregation powering BOTH the ``/panel/data`` page
and the ``data_stats`` MCP tool — same semantics, both surfaces (spec
crawl-control-center: the aggregation is shared and mutates nothing).
Separated from ``snapshot.py`` so the snapshot/digest contract stays
digest-stable while coverage evolves.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from fd_open_data_mcp.models import (
    Concept, ConceptCoverage, ConceptSourceStats, DataCensus, SemanticObservation,
)
from fd_open_data_mcp.visibility.census import LOCAL_STORE

# Single-flight key for the coverage-cache refresh across application
# instances sharing one database (panel-data-coverage-cache design D3).
# Transaction-scoped: held from acquisition until the refresh transaction
# commits. Dedicated to this refresh — do not reuse for other locks.
COVERAGE_REFRESH_LOCK_KEY = 8_844_332_211


def _naive_utc(dt: datetime | None) -> datetime | None:
    """Normalize to naive UTC — SQLite DateTime columns return naive values
    while write sites pass timezone-aware ``now``; mixing them in a Python
    max() raises. Storage convention for last_fetch stays naive UTC."""
    if dt is not None and dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def bump_rollups(
    session: Session,
    concept_id: int,
    entity_type: str,
    entity_id: int,
    source_used: str,
    inserted_dates: list[str],
    touched_dates: list[str],
    now: datetime,
) -> None:
    """Incremental rollup maintenance, called INSIDE the observation-write
    transaction (add-data-stream-engine task 2.3 — replaces the hourly
    full-table refresh; cost O(batch), never O(table)).

    ``inserted_dates``: points newly INSERTED for this (concept, source) —
    each raises the pair's row count and may move latest_date.
    ``touched_dates``: points INSERTED *or* UPDATED (value refreshed) —
    any of them may move latest_date; updated rows also bump last_fetch.

    Coexistence caliber: a point covered by MORE THAN ONE source counts as
    ONE point in ``concept_coverage.rows`` (add-multi-source-observations).
    Maintained exactly, not approximated: for every inserted point we check
    whether the same point exists under a DIFFERENT source (one indexed
    lookup per call site batch — the call sites pass the whole batch so the
    check is one query, not one per point) and keep a running
    ``coexisting_points`` counter on the coverage row; rows = sum(stats.rows)
    - coexisting_points stays exact even if cross-source coexistence appears
    (census 2026-11-04: zero today).
    """
    stats = (
        session.query(ConceptSourceStats)
        .filter_by(concept_id=concept_id, source_used=source_used)
        .with_for_update()
        .one_or_none()
    )
    if stats is None:
        stats = ConceptSourceStats(
            concept_id=concept_id, source_used=source_used, rows=0,
        )
        session.add(stats)
    stats.rows += len(inserted_dates)
    candidates = [d for d in (*inserted_dates, *touched_dates) if d]
    if candidates:
        top = max(candidates)  # canonical YYYY-MM-DD: lexicographic == chronological
        if stats.latest_date is None or top > stats.latest_date:
            stats.latest_date = top
    now_n = _naive_utc(now)
    if stats.last_fetch is None or now_n > stats.last_fetch:
        stats.last_fetch = now_n

    # Coexistence probe FIRST (one indexed query per call-site batch): every
    # inserted point already held under a DIFFERENT source. Runs before the
    # coverage row is materialized so autoflush never sees a half-built row.
    coexisting_delta = 0
    if inserted_dates:
        others = session.execute(
            select(SemanticObservation.date)
            .where(
                SemanticObservation.concept_id == concept_id,
                SemanticObservation.entity_type == entity_type,
                SemanticObservation.entity_id == entity_id,
                SemanticObservation.date.in_(inserted_dates),
                SemanticObservation.source_used != source_used,
            )
        ).scalars()
        coexisting_delta = len(set(others))

    cov = (
        session.query(ConceptCoverage)
        .filter_by(concept_id=concept_id)
        .with_for_update()
        .one_or_none()
    )
    if cov is None:
        cov = ConceptCoverage(concept_id=concept_id, rows=0, sources=0,
                              coexisting_points=coexisting_delta)
        session.add(cov)
    else:
        cov.coexisting_points = (cov.coexisting_points or 0) + coexisting_delta
    _project_coverage(session, concept_id, cov=cov)


def _project_coverage(session: Session, concept_id: int,
                      cov: ConceptCoverage | None = None) -> None:
    """Recompute one concept's ``concept_coverage`` row FROM
    ``concept_source_stats`` (a handful of rows) — the projection that makes
    coverage reads never touch ``semantic_observations``. ``cov`` passes the
    already-located row through (a pending new row would be invisible to the
    query, so the caller that just created it must hand it over)."""
    # Stats query FIRST: it autoflushes, and a pending coverage row whose
    # fields are not set yet would violate its NOT NULLs — so locate/create
    # cov only AFTER the last query that can autoflush it, with explicit
    # safe defaults at construction.
    stats = (
        session.query(ConceptSourceStats)
        .filter_by(concept_id=concept_id)
        .all()
    )
    if cov is None:
        cov = session.query(ConceptCoverage).filter_by(concept_id=concept_id).one_or_none()
    if cov is None:
        cov = ConceptCoverage(concept_id=concept_id, rows=0, sources=0,
                              coexisting_points=0)
        session.add(cov)
    cov.rows = sum(s.rows for s in stats) - (cov.coexisting_points or 0)
    cov.latest_date = max((s.latest_date for s in stats if s.latest_date),
                          default=None)
    cov.last_fetch = max((_naive_utc(s.last_fetch) for s in stats
                          if s.last_fetch), default=None)
    cov.sources = sum(1 for s in stats if s.rows > 0)
    cov.computed_at = datetime.now(timezone.utc)


def rebuild_concept_rollups(session: Session, concept_id: int | None = None) -> dict:
    """Rebuild the rollup tables from row-level observations (task 2.4).

    Full recompute of ``concept_source_stats`` (optionally one concept) and,
    from it, every touched concept's ``concept_coverage`` projection —
    including a fresh coexisting_points count (exact GROUP BY, the same
    machinery as ``coverage_by_concept``, ~8 s at 6.3M rows). The rebuild is
    the ground truth the incremental hook is verified against; safe to run
    on a live system (plain SELECTs + upserts of derived rows).
    """
    filters = []
    if concept_id is not None:
        filters.append(SemanticObservation.concept_id == concept_id)

    per_source = (
        session.query(
            SemanticObservation.concept_id.label("concept_id"),
            SemanticObservation.source_used.label("source_used"),
            func.count().label("rows"),
            func.max(SemanticObservation.date).label("latest_date"),
            func.max(SemanticObservation.fetched_at).label("last_fetch"),
        )
        .where(*filters)
        .group_by(SemanticObservation.concept_id, SemanticObservation.source_used)
        .all()
    )
    touched = {r.concept_id for r in per_source}
    if concept_id is not None:
        touched.add(concept_id)

    cached = {
        (s.concept_id, s.source_used): s
        for s in session.query(ConceptSourceStats)
        .filter(*([ConceptSourceStats.concept_id == concept_id]
                  if concept_id is not None else []))
        .all()
    }
    for r in per_source:
        s = cached.pop((r.concept_id, r.source_used), None)
        if s is None:
            s = ConceptSourceStats(concept_id=r.concept_id,
                                   source_used=r.source_used)
            session.add(s)
        s.rows = int(r.rows)
        s.latest_date = r.latest_date
        s.last_fetch = _naive_utc(r.last_fetch)
    for stale in cached.values():  # pair vanished (row-level delete) → drop
        session.delete(stale)
    session.flush()

    # Exact coexistence count per touched concept (five-column GROUP BY —
    # the ~8 s point machinery, scoped to touched concepts only).
    coexisting: dict[int, int] = {}
    if touched:
        points = (
            select(
                SemanticObservation.concept_id.label("concept_id"),
                SemanticObservation.entity_type,
                SemanticObservation.entity_id,
                SemanticObservation.date,
                SemanticObservation.granularity,
                func.count().label("srcs"),
            )
            .where(SemanticObservation.concept_id.in_(touched))
            .group_by(
                SemanticObservation.concept_id,
                SemanticObservation.entity_type,
                SemanticObservation.entity_id,
                SemanticObservation.date,
                SemanticObservation.granularity,
            )
            .subquery()
        )
        for cid, n in session.execute(
            select(points.c.concept_id, func.coalesce(func.sum(points.c.srcs - 1), 0))
            .where(points.c.srcs > 1)
            .group_by(points.c.concept_id)
        ):
            coexisting[cid] = int(n)

    now = datetime.now(timezone.utc)
    covs = {c.concept_id: c for c in
            session.query(ConceptCoverage)
            .filter(ConceptCoverage.concept_id.in_(touched)).all()
            } if touched else {}
    for cid in touched:
        cov = covs.get(cid)
        if cov is None:
            cov = ConceptCoverage(concept_id=cid, rows=0, sources=0,
                                  coexisting_points=coexisting.get(cid, 0))
            session.add(cov)
        else:
            cov.coexisting_points = coexisting.get(cid, 0)
        _project_coverage(session, cid, cov=cov)
        cov.computed_at = now
    # Concepts whose observations vanished entirely: coverage rows go away
    # (matches the retired refresh's cleanup semantics) when rebuilding all.
    if concept_id is None:
        for c in session.query(ConceptCoverage).all():
            if c.concept_id not in touched:
                session.delete(c)
    session.commit()
    return {"status": "rebuilt", "concepts": len(touched),
            "pairs": len(per_source)}


def coverage_by_concept(
    session: Session,
    concept_id: int | None = None,
    entity_type: str | None = None,
) -> list[dict]:
    """Per-concept observation coverage: covered-point count, latest
    observation date, distinct sources used, most recent fetch.

    A covered point is one (concept_id, entity_type, entity_id, date,
    granularity) tuple: coexisting sources of the same point count as ONE
    point (add-multi-source-observations), so the point count comes from a
    five-column GROUP BY — never from DISTINCT over a per-row string
    concatenation, whose aggregate machinery measured ~102 s on the 6.3M-row
    master where the point-column group-by runs in ~8 s (identical result:
    the current data holds zero coexisting points, and the group-by keeps
    the dedup semantics if coexistence appears). ``latest_date`` is the
    canonical YYYY-MM-DD string, so ``max(date)`` is both lexicographic and
    chronological. Ordered by point count descending. Read-only: a plain
    aggregate, no table writes.
    """
    filters = []
    if concept_id is not None:
        filters.append(SemanticObservation.concept_id == concept_id)
    if entity_type:
        filters.append(SemanticObservation.entity_type == entity_type)

    obs = SemanticObservation
    points = (
        select(obs.concept_id, obs.entity_type, obs.entity_id, obs.date,
               obs.granularity)
        .where(*filters)
        .distinct()
        .subquery()
    )
    pts = (
        select(
            points.c.concept_id.label("concept_id"),
            func.count().label("rows"),
            func.max(points.c.date).label("latest_date"),
        )
        .group_by(points.c.concept_id)
        .subquery()
    )
    meta = (
        select(
            obs.concept_id.label("concept_id"),
            func.max(obs.fetched_at).label("last_fetch"),
            func.count(func.distinct(obs.source_used)).label("sources"),
        )
        .where(*filters)
        .group_by(obs.concept_id)
        .subquery()
    )
    q = (
        session.query(
            pts.c.concept_id.label("concept_id"),
            pts.c.rows.label("rows"),
            pts.c.latest_date.label("latest_date"),
            meta.c.last_fetch.label("last_fetch"),
            meta.c.sources.label("sources"),
            Concept.code.label("code"),
            Concept.name_en.label("name_en"),
            Concept.name_zh.label("name_zh"),
            Concept.category.label("category"),
        )
        .outerjoin(meta, meta.c.concept_id == pts.c.concept_id)
        .outerjoin(Concept, Concept.id == pts.c.concept_id)
    )
    out = []
    for r in q.all():
        out.append({
            "concept_id": r.concept_id,
            "code": r.code,
            "name_en": r.name_en,
            "name_zh": r.name_zh,
            "category": r.category,
            "rows": int(r.rows),
            "latest_date": r.latest_date,
            "last_fetch": r.last_fetch.isoformat() if r.last_fetch else None,
            "sources": int(r.sources),
        })
    out.sort(key=lambda x: x["rows"], reverse=True)
    return out


def refresh_concept_coverage(session: Session) -> dict:
    """RETIRED periodic full-table refresh (add-data-stream-engine task 2.5)
    — kept as a thin alias to the exact rebuild for any stale caller, with
    a compatible return shape. Production paths now use: the in-transaction
    write hook (:func:`bump_rollups`) for steady state, and the explicit
    rebuild (:func:`rebuild_concept_rollups`, exposed as
    ``fd-open-data-mcp rebuild-rollups`` and the panel refresh action) as
    the ground truth."""
    out = rebuild_concept_rollups(session)
    # rows_total: sum over the rebuilt coverage rows (the retired refresh's
    # caliber — covered points per concept, summed).
    rows_total = 0
    if out["concepts"]:
        rows_total = sum(int(c.rows or 0) for c in session.query(ConceptCoverage).all())
    return {"status": "refreshed", "concepts": out["concepts"],
            "rows_total": rows_total}


def storage_watermark(session: Session) -> dict:
    """Storage watermark panel data (add-data-stream-engine task 2.7).

    Page-safe by construction: every figure comes from a SMALL table —
    concept_coverage (one row per covered concept), concept_source_stats
    (one row per concept×source pair), and the cached data_census row (the
    heavy obs_month distribution was computed once by the census refresh
    action, never here). No semantic_observations scan appears on this path;
    that is the whole point of the rollup tiering.

    Returns:
      rollup: freshness (max computed_at + age_hours), coverage concept
        count, total covered points, coexisting-point total
      drift: concepts whose stats/coverage rows disagree — the incremental
        hook is the ONLY writer of both, so any disagreement means the hook
        stopped running (rebuild is the fix). Also flags impossible-state
        rows (negative point counts).
      store: cached census figures for the local store (rows, size,
        obs_month distribution incl. the _unmappable default-partition
        count) — NULL until a census refresh has run.
    """
    cov = session.query(ConceptCoverage).all()
    stats = session.query(ConceptSourceStats).all()

    now = datetime.now(timezone.utc)
    computed_ats = [c.computed_at for c in cov if c.computed_at is not None]
    age_hours = None
    if computed_ats:
        t = max(computed_ats)
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        age_hours = round((now - t).total_seconds() / 3600, 1)

    stats_concepts = {s.concept_id for s in stats if s.rows > 0}
    cov_concepts = {c.concept_id for c in cov}
    drift = {
        "stats_without_coverage": sorted(stats_concepts - cov_concepts),
        "coverage_without_stats": sorted(cov_concepts - stats_concepts),
        "negative_point_rows": sorted(
            c.concept_id for c in cov if (c.rows or 0) < 0),
    }
    drift["hook_healthy"] = not any(
        drift[k] for k in ("stats_without_coverage", "coverage_without_stats",
                           "negative_point_rows"))

    census = session.query(DataCensus).filter_by(store=LOCAL_STORE).first()
    store = None
    if census is not None:
        store = {
            "rows": census.approx_rows,
            "size_bytes": census.total_size_bytes,
            "obs_month_dist": census.obs_month_dist,
            "sampled_at": census.sampled_at.isoformat() if census.sampled_at else None,
            "error": census.error,
        }

    return {
        "rollup": {
            "concepts_covered": len(cov_concepts),
            "points_total": sum(max(c.rows or 0, 0) for c in cov),
            "coexisting_points_total": sum(c.coexisting_points or 0 for c in cov),
            "computed_at": max(computed_ats).isoformat() if computed_ats else None,
            "age_hours": age_hours,
        },
        "drift": drift,
        "store": store,
    }


def cached_concept_coverage(session: Session) -> dict:
    """Read the ``concept_coverage`` cache for ``/panel/data`` and the
    ``data_stats`` unfiltered detail: per-concept rows in the same shape as
    :func:`coverage_by_concept` (plus per-row ``computed_at``), the global
    sample time (max computed_at), and its age in hours. An empty cache
    returns no rows — the caller's bounded live fallback applies
    (panel-data-coverage-cache design D5).
    """
    q = (
        session.query(ConceptCoverage, Concept.code, Concept.name_en,
                      Concept.name_zh, Concept.category)
        .outerjoin(Concept, Concept.id == ConceptCoverage.concept_id)
        .all()
    )
    rows = [{
        "concept_id": cov.concept_id,
        "code": code,
        "name_en": name_en,
        "name_zh": name_zh,
        "category": category,
        "rows": cov.rows,
        "latest_date": cov.latest_date,
        "last_fetch": cov.last_fetch.isoformat() if cov.last_fetch else None,
        "sources": cov.sources,
        "computed_at": cov.computed_at,
    } for cov, code, name_en, name_zh, category in q]
    rows.sort(key=lambda x: x["rows"], reverse=True)
    sampled_at = None
    age_hours = None
    if rows:
        sampled_at = max(r["computed_at"] for r in rows)
        t = sampled_at if sampled_at.tzinfo is not None else \
            sampled_at.replace(tzinfo=timezone.utc)
        age_hours = round(
            (datetime.now(timezone.utc) - t).total_seconds() / 3600, 1)
    return {"concepts": rows, "sampled_at": sampled_at,
            "age_hours": age_hours}

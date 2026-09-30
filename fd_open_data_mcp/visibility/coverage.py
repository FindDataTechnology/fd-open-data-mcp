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

from fd_open_data_mcp.models import Concept, ConceptCoverage, SemanticObservation

# Single-flight key for the coverage-cache refresh across application
# instances sharing one database (panel-data-coverage-cache design D3).
# Transaction-scoped: held from acquisition until the refresh transaction
# commits. Dedicated to this refresh — do not reuse for other locks.
COVERAGE_REFRESH_LOCK_KEY = 8_844_332_211


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
    """Recompute and upsert the ``concept_coverage`` cache from the live
    aggregate; removes rows for concepts that no longer have observations.

    Single-flight: on PostgreSQL the whole refresh runs inside one
    transaction holding ``pg_try_advisory_xact_lock`` on
    ``COVERAGE_REFRESH_LOCK_KEY``; an instance that cannot take the lock
    skips the cycle (another instance is mid-refresh). Non-PostgreSQL
    backends (unit tests) run unlocked.
    """
    if session.bind is not None and session.bind.dialect.name == "postgresql":
        locked = session.execute(
            select(func.pg_try_advisory_xact_lock(COVERAGE_REFRESH_LOCK_KEY))
        ).scalar()
        if not locked:
            return {"status": "skipped", "reason": "lock-held"}

    rows = coverage_by_concept(session)
    now = datetime.now(timezone.utc)
    keep = {r["concept_id"] for r in rows}
    cached = {c.concept_id: c
              for c in session.query(ConceptCoverage).all()}
    for cid, c in cached.items():
        if cid not in keep:
            session.delete(c)
    for r in rows:
        c = cached.get(r["concept_id"])
        if c is None:
            c = ConceptCoverage(concept_id=r["concept_id"])
            session.add(c)
        c.rows = r["rows"]
        c.latest_date = r["latest_date"]
        c.last_fetch = (datetime.fromisoformat(r["last_fetch"])
                        if r["last_fetch"] else None)
        c.sources = r["sources"]
        c.computed_at = now
    session.commit()
    return {"status": "refreshed", "concepts": len(rows),
            "rows_total": sum(r["rows"] for r in rows)}


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

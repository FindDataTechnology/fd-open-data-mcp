"""Data-coverage aggregation over ``semantic_observations``
(add-panel-crawl-observability D4).

One read-only GROUP BY powering BOTH the ``/panel/data`` page and the
``data_stats`` MCP tool — same query, both surfaces (spec crawl-control-center:
the aggregation is shared and mutates nothing). Separated from
``snapshot.py`` so the snapshot/digest contract stays digest-stable while
coverage evolves.
"""
from __future__ import annotations

from sqlalchemy import func
from sqlalchemy.orm import Session

from fd_open_data_mcp.models import Concept, SemanticObservation


def coverage_by_concept(
    session: Session,
    concept_id: int | None = None,
    entity_type: str | None = None,
) -> list[dict]:
    """Per-concept observation coverage: row count, latest observation date,
    distinct sources used, most recent fetch.

    ``date`` is the canonical YYYY-MM-DD string, so ``max(date)`` is both
    lexicographic and chronological. Ordered by row count descending.
    Read-only: a plain aggregate, no table writes.
    """
    q = (
        session.query(
            SemanticObservation.concept_id.label("concept_id"),
            func.count(SemanticObservation.id).label("rows"),
            func.max(SemanticObservation.date).label("latest_date"),
            func.max(SemanticObservation.fetched_at).label("last_fetch"),
            func.count(func.distinct(SemanticObservation.source_used)).label("sources"),
            Concept.code.label("code"),
            Concept.name_en.label("name_en"),
            Concept.name_zh.label("name_zh"),
            Concept.category.label("category"),
        )
        .outerjoin(Concept, SemanticObservation.concept_id == Concept.id)
        .group_by(
            SemanticObservation.concept_id,
            Concept.code, Concept.name_en, Concept.name_zh, Concept.category,
        )
    )
    if concept_id is not None:
        q = q.filter(SemanticObservation.concept_id == concept_id)
    if entity_type:
        q = q.filter(SemanticObservation.entity_type == entity_type)

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

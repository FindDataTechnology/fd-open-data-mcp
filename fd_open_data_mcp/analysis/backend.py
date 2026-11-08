"""Analysis backends (task 5.1): a swappable read path over the rollup tier.

The analysis API answers two query shapes through one ``AnalysisBackend``:

  - ``concept_summary``  per-concept coverage aggregate (rows / latest_date /
    sources / last_fetch). The DEFAULT backend reads the rollup tier
    (``concept_coverage``) — O(concepts), never a row-level scan. This is the
    structural successor to the retired hourly full-table refresh.
  - ``time_series``      one concept's values over a bounded date range. A
    single-concept scan is indexed (``ix_concept_id``) and bounded, so it is
    cheap even though it reads row level — the 102s problem was an UNBOUNDED
    GROUP BY over every concept, which the summary path now avoids.

``RollupBackend`` is the default and the zero-dependency implementation: the
spec's "内嵌列存零部署可用" is satisfied by reading the rollup the write hook
already maintains — no external service, no extra install. A columnar store
(DuckDB / ClickHouse) is a FUTURE profile: register it with
``register_backend("duckdb", factory)`` and select it via
``FD_ODM_ANALYSIS_BACKEND``; the api contract is unchanged by the swap. The
registry is the seam — this module does not pre-bake a half-implemented
columnar path it cannot test.

Backend selection: ``FD_ODM_ANALYSIS_BACKEND`` (default ``rollup``). An
unregistered kind falls back to rollup with a warning rather than crashing a
query — the analysis plane must degrade to a working read, not error out.
"""
from __future__ import annotations

import logging
import os
from abc import ABC, abstractmethod
from typing import Callable

from sqlalchemy import exists
from sqlalchemy.orm import Session

from fd_open_data_mcp.entities.resolver import SECURITY_ENTITY_TYPES
from fd_open_data_mcp.models import Concept, ConceptCoverage, Entity, SemanticObservation

logger = logging.getLogger(__name__)


class AnalysisBackend(ABC):
    @abstractmethod
    def concept_summary(self, session: Session, *, concept_id: int | None = None,
                        entity_type: str | None = None) -> list[dict]: ...

    @abstractmethod
    def time_series(self, session: Session, *, concept_id: int,
                    entity_type: str, entity_id: int,
                    start: str | None, end: str | None,
                    limit: int) -> list[dict]: ...


class RollupBackend(AnalysisBackend):
    """Default backend: rollup for summaries, indexed bounded scan for series."""

    def concept_summary(self, session, *, concept_id=None, entity_type=None):
        q = (
            session.query(ConceptCoverage, Concept)
            .outerjoin(Concept, Concept.id == ConceptCoverage.concept_id)
        )
        if concept_id is not None:
            q = q.filter(ConceptCoverage.concept_id == concept_id)
        if entity_type:
            q = q.filter(Concept.entity_type == entity_type)
        out = []
        for cov, c in q.all():
            out.append({
                "concept_id": cov.concept_id,
                "code": c.code if c else None,
                "entity_type": c.entity_type if c else None,
                "rows": cov.rows,
                "latest_date": cov.latest_date,
                "last_fetch": cov.last_fetch.isoformat() if cov.last_fetch else None,
                "sources": cov.sources,
                "computed_at": cov.computed_at.isoformat() if cov.computed_at else None,
            })
        out.sort(key=lambda r: r["rows"], reverse=True)
        return out

    def time_series(self, session, *, concept_id, entity_type, entity_id,
                    start, end, limit):
        q = (
            session.query(SemanticObservation)
            .filter(SemanticObservation.concept_id == concept_id,
                    SemanticObservation.entity_type == entity_type,
                    SemanticObservation.entity_id == entity_id)
        )
        if entity_type in SECURITY_ENTITY_TYPES:
            # The row's declared type is 'stock' for 343,411 price/financials
            # rows that hang off person/industry/fund entities (measured
            # 2026-11-08); only the entity's own class can tell them apart.
            q = q.filter(exists().where(
                Entity.id == SemanticObservation.entity_id,
                Entity.entity_type == entity_type,
            ))
        if start:
            q = q.filter(SemanticObservation.date >= start)
        if end:
            q = q.filter(SemanticObservation.date <= end)
        rows = q.order_by(SemanticObservation.date).limit(limit).all()
        # collapse multi-source per point to the first-seen (preferred) row —
        # same point caliber as coverage_by_concept (one point, any source)
        by_point: dict[tuple, dict] = {}
        for r in rows:
            key = (r.date, r.granularity)
            if key not in by_point:
                by_point[key] = {
                    "date": r.date, "granularity": r.granularity,
                    "value": r.value, "unit": r.unit, "source_used": r.source_used,
                }
        return [by_point[k] for k in sorted(by_point)]


_REGISTRY: dict[str, Callable[[], AnalysisBackend]] = {"rollup": RollupBackend}


def register_backend(kind: str, factory: Callable[[], AnalysisBackend]) -> None:
    """Extension seam for a columnar profile (duckdb/clickhouse)."""
    _REGISTRY[kind] = factory


def get_backend() -> AnalysisBackend:
    kind = (os.environ.get("FD_ODM_ANALYSIS_BACKEND") or "rollup").lower()
    factory = _REGISTRY.get(kind)
    if factory is None:
        logger.warning("unknown analysis backend %r — using rollup", kind)
        factory = RollupBackend
    return factory()

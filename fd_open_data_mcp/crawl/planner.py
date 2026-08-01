"""The crawl planner: concepts + entity scope + date range -> ``CrawlPlan``.

Reuses the same primitives ``read()`` uses (design.md, spec crawl-planner):
``rank_sources_for_concept`` for source order, ``_bindings_for_source`` for the
function+column bindings, ``resolve_identifier`` for per-source entity ids. Does NOT
duplicate ranking/binding/identity logic.

Lazy (D6): the plan carries scope + filters; the executor expands
``(concept x entity x date)`` at crawl time. The planner only validates identifier
coverage for an explicit ``entity_ids`` list; a filter scope is deferred to the
executor (it cannot be enumerated cheaply without the entities DB).
"""
from __future__ import annotations

from sqlalchemy.orm import Session

from fd_open_data_mcp.crawl.plan import (
    CrawlPlan, DateRange, EntityScope, PlanConcept, PlanSource,
)
from fd_open_data_mcp.entities.resolver import resolve_identifier
from fd_open_data_mcp.fetch.dispatch import _bindings_for_source
from fd_open_data_mcp.models import Concept
from fd_open_data_mcp.ranking.scorer import rank_sources_for_concept


def plan_crawl(
    session: Session,
    concept_ids: list[int],
    entity_scope: EntityScope,
    date_range: DateRange,
) -> CrawlPlan:
    """Compile a wanted-concept spec into a ``CrawlPlan``.

    Concepts with no confirmed binding (or an entity_type mismatch) are refused and
    reported in ``plan.unroutable`` rather than silently dropped. For an explicit
    ``entity_scope.entity_ids`` list, ``(entity, source)`` pairs with no identifier
    are reported in ``plan.unmapped`` (graceful degradation).
    """
    wanted: list[PlanConcept] = []
    unroutable: list[dict] = []

    for cid in concept_ids:
        concept = session.get(Concept, cid)
        if concept is None:
            unroutable.append({"concept_id": cid, "reason": "concept not found"})
            continue
        if concept.entity_type != entity_scope.entity_type:
            unroutable.append({
                "concept_id": cid, "code": concept.code,
                "reason": (f"entity_type mismatch: concept is {concept.entity_type}, "
                           f"scope is {entity_scope.entity_type}"),
            })
            continue

        ranked = rank_sources_for_concept(session, cid, date_range.start)
        sources: list[PlanSource] = []
        for cand in ranked:
            src = cand["source"]
            for binding, fn in _bindings_for_source(session, cid, src):
                sources.append(PlanSource(
                    source=src, score=cand["score"],
                    function_id=fn.id, function_command=fn.command,
                    column_name=binding.column.name, binding_id=binding.id,
                    confidence=binding.confidence,
                ))
        if not sources:
            unroutable.append({
                "concept_id": cid, "code": concept.code,
                "reason": "no confirmed binding / no candidate source",
            })
            continue
        wanted.append(PlanConcept(
            concept_id=cid, code=concept.code, entity_type=concept.entity_type,
            unit=concept.unit, frequency=concept.frequency, ranked_sources=sources,
        ))

    unmapped = _identifier_coverage(session, wanted, entity_scope)

    return CrawlPlan(
        wanted_concepts=wanted,
        entity_scope=entity_scope,
        date_range=date_range,
        unroutable=unroutable,
        unmapped=unmapped,
    )


def _identifier_coverage(
    session: Session, wanted: list[PlanConcept], scope: EntityScope,
) -> list[dict]:
    """For an explicit entity-id list, report ``(entity, source)`` pairs with no
    identifier. A filter scope (``entity_ids is None``) is deferred to the executor."""
    if not scope.entity_ids:
        return []
    seen: set[tuple[int, str, int]] = set()
    out: list[dict] = []
    for concept in wanted:
        for ps in concept.ranked_sources:
            for eid in scope.entity_ids:
                ident = resolve_identifier(session, concept.entity_type, eid, ps.source)
                if ident is None:
                    key = (eid, ps.source, concept.concept_id)
                    if key in seen:
                        continue
                    seen.add(key)
                    out.append({
                        "entity_id": eid, "source": ps.source,
                        "concept_id": concept.concept_id,
                    })
    return out

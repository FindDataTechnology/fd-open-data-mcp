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

import datetime as dt

from sqlalchemy import text
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
    since_last: bool = False,
    source_filter: list[str] | None = None,
    mode: str = "per_date",
) -> CrawlPlan:
    """Compile a wanted-concept spec into a ``CrawlPlan``.

    Concepts with no confirmed binding (or an entity_type mismatch) are refused and
    reported in ``plan.unroutable`` rather than silently dropped. For an explicit
    ``entity_scope.entity_ids`` list, ``(entity, source)`` pairs with no identifier
    are reported in ``plan.unmapped`` (graceful degradation).

    If ``since_last`` is True, the plan's ``date_range.start`` is derived from the
    minimum per-concept ``max(date)`` already in ``semantic_observations`` (across all
    sources — the table dedups by ``(concept, entity, date)`` with no ``source_used``
    in the unique key), advanced one period forward by the plan's frequency. If all
    concepts have no prior observations, the explicit ``date_range.start`` is used
    (or the concepts are refused as unroutable if no start is available).

    If ``source_filter`` is provided, only sources in the list are included in
    ``ranked_sources`` (useful for restricting crawls to specific providers).

    ``mode`` is ``per_date`` (default; one request per concept x entity x date) or
    ``series`` (one request per concept x entity against a bulk_history endpoint; the
    executor explodes the returned frame into per-date observations). In ``series``
    mode only bindings to ``bulk_history=True`` functions are kept; a concept with no
    such binding is refused as unroutable for series mode (no requests emitted).
    """
    if mode not in ("per_date", "series"):
        raise ValueError(f"mode must be 'per_date' or 'series', got {mode!r}")
    # since_last derives date_range.start from the per-concept watermark — but ONLY when
    # no explicit start was given (explicit start wins, per spec). CLI/MCP also clear
    # since_last when --start is set; this guard makes the planner robust on direct call.
    unroutable: list[dict] = []
    if since_last and date_range.start is None:
        watermarks = [
            _watermark(session, cid, entity_scope.entity_type)
            for cid in concept_ids
        ]
        non_none = [w for w in watermarks if w is not None]
        if non_none:
            min_wm = min(non_none)
            new_start = _next_period_start(min_wm, date_range.frequency)
            date_range = DateRange(
                start=new_start, end=date_range.end, frequency=date_range.frequency,
            )
        else:
            # All concepts have no prior data and no explicit start → refuse all
            unroutable.extend([
                {"concept_id": cid, "reason": "no prior observations for --since-last, need explicit --start"}
                for cid in concept_ids
            ])
            return CrawlPlan(
                wanted_concepts=[], entity_scope=entity_scope, date_range=date_range,
                unroutable=unroutable, unmapped=[], mode=mode,
            )

    wanted: list[PlanConcept] = []

    for cid in concept_ids:
        concept = session.get(Concept, cid)
        if concept is None:
            unroutable.append({"concept_id": cid, "reason": "concept not found"})
            continue
        if concept.deprecated:
            from fd_open_data_mcp.entities.resolver import find_canonical_replacement
            repl = find_canonical_replacement(session, concept)
            unroutable.append({
                "concept_id": cid, "code": concept.code,
                "reason": "concept is deprecated",
                "canonical_replacement": (repl.code if repl else None),
                "canonical_replacement_id": (repl.id if repl else None),
            })
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
            if source_filter and src not in source_filter:
                continue
            for binding, fn in _bindings_for_source(session, cid, src):
                if mode == "series" and not fn.bulk_history:
                    # series mode fetches the whole history in one call — only
                    # bulk_history endpoints can serve it (design D6)
                    continue
                sources.append(PlanSource(
                    source=src, score=cand["score"],
                    function_id=fn.id, function_command=fn.command,
                    column_name=binding.column.name, binding_id=binding.id,
                    confidence=binding.confidence,
                ))
        if not sources:
            unroutable.append({
                "concept_id": cid, "code": concept.code,
                "reason": ("no bulk_history source for series mode" if mode == "series"
                           else "no confirmed binding / no candidate source"),
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
        mode=mode,
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


def _watermark(session: Session, concept_id: int, entity_type: str) -> str | None:
    """Return max(date) for this concept (across all sources), or None if no observations.

    The observation table's UniqueConstraint has no ``source_used``, so there's exactly
    one row per ``(concept, entity, date)`` regardless of source. The watermark is the
    newest observation for this concept.
    """
    row = session.execute(
        text("SELECT max(date) FROM semantic_observations "
             "WHERE concept_id=:c AND entity_type=:et"),
        {"c": concept_id, "et": entity_type},
    ).first()
    return row[0] if row and row[0] else None


def _next_period_start(watermark_date_str: str, frequency: str | None) -> str:
    """Advance the watermark date by one period (yearly/monthly/daily).

    Handles watermark formats: 'YYYY' (year-only), 'YYYY-MM' (year-month), 'YYYY-MM-DD'.
    Returns the ISO date string for the start of the next period.
    """
    # Parse the watermark date
    if len(watermark_date_str) == 4:  # 'YYYY'
        wm = dt.date(int(watermark_date_str), 1, 1)
    elif len(watermark_date_str) == 7:  # 'YYYY-MM'
        wm = dt.date.fromisoformat(watermark_date_str + "-01")
    else:  # 'YYYY-MM-DD' or longer
        wm = dt.date.fromisoformat(watermark_date_str[:10])

    # Advance by frequency
    if frequency == "yearly":
        nxt = dt.date(wm.year + 1, 1, 1)
    elif frequency == "monthly":
        nxt = (wm.replace(day=1) + dt.timedelta(days=32)).replace(day=1)
    else:  # daily or None
        nxt = wm + dt.timedelta(days=1)

    return nxt.isoformat()

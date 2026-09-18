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
import os

from sqlalchemy import bindparam, func, text
from sqlalchemy.orm import Session

from fd_open_data_mcp.crawl.plan import (
    CrawlPlan, DateRange, EntityScope, PlanConcept, PlanSource,
)
from fd_open_data_mcp.entities.resolver import resolve_identifier
from fd_open_data_mcp.fetch.dispatch import _bindings_for_source
from fd_open_data_mcp.fetch.suppress import suppressed_paths
from fd_open_data_mcp.fetch.capability import (
    UNRESOLVABLE, check_command,
)
from fd_open_data_mcp.models import Concept
from fd_open_data_mcp.ranking.scorer import rank_sources_for_concept


def _candidate_pairs(session: Session, concept_ids: list[int]) -> set[tuple[int, int]]:
    """Every ``(concept_id, function_id)`` the requested concepts could route to.

    Bounded by ``concept_bindings`` (a few thousand rows), so computing the
    suppressed set up front costs one small query rather than one per cell.
    """
    from fd_open_data_mcp.models import ConceptBinding, FunctionColumn

    rows = (
        session.query(ConceptBinding.concept_id, FunctionColumn.function_id)
        .join(FunctionColumn, FunctionColumn.id == ConceptBinding.column_id)
        .filter(ConceptBinding.concept_id.in_(list(concept_ids)))
        .all()
    )
    return {(concept_id, function_id) for concept_id, function_id in rows}


def capability_check_enabled() -> bool:
    """Whether an endpoint that cannot be resolved here is excluded at plan time.

    Report-only by default, like the eligibility gate: a host without the crawl
    image's libraries resolves nothing (``UNVERIFIABLE``), and enforcing there
    would be inert at best — so the switch is explicit and separate.
    """
    return (os.environ.get("FD_CAPABILITY_CHECK") or "report").strip().lower() == "enforce"


def chain_bound() -> int:
    """Maximum candidates in one concept's failover chain (spec concept-fetch).

    A chain is built from every binding for the concept, in rank order, with no
    cap. Concept ``price.close``/``stock`` reached 117 entries, and with three
    proxies times one retry per proxy a single cell could issue several hundred
    upstream calls before giving up. Candidates past the bound are recorded as
    not-attempted rather than dropped silently.
    """
    try:
        return max(1, int(os.environ.get("FD_CRAWL_CHAIN_MAX", "8")))
    except ValueError:
        return 8


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
    # (concept_id, source) pairs whose bound function carries bulk_snapshot —
    # resolved to a subset check once the unmapped report exists (D6)
    snapshot_candidates: set[tuple[int, str]] = set()
    # since_last derives date_range.start from the per-concept watermark — but ONLY when
    # no explicit start was given (explicit start wins, per spec). CLI/MCP also clear
    # since_last when --start is set; this guard makes the planner robust on direct call.
    concepts = {cid: session.get(Concept, cid) for cid in concept_ids}

    # Permanent-path suppression (spec concept-fetch). Computed ONCE for the
    # whole plan, not per cell: a (concept, function) whose recent outcomes are
    # all permanent cannot succeed from ANY egress, so unlike a demotion it is
    # excluded outright. Applied before the chain bound so the bound is filled
    # with candidates that can run.
    suppressed = suppressed_paths(session, _candidate_pairs(session, concept_ids))
    suppressed_report: list[dict] = []
    enforce_capability = capability_check_enabled()
    capability_cache: dict[tuple[str, str], str] = {}
    unroutable: list[dict] = []
    truncated: list[dict] = []
    if since_last and date_range.start is None:
        # per-concept watermark at its own granularity: a monthly concept's since-last
        # advances from its monthly observations, never from a daily row on the 1st
        # (fix-observation-time-granularity). Scoped to the plan's source_filter when
        # set (add-multi-source-observations): a second source's backfill plan must
        # start from what THAT source holds — rows another source crawled are not
        # its coverage.
        watermarks = []
        for cid in concept_ids:
            c = concepts.get(cid)
            gran = _granularity_for(c.frequency) if c else "day"
            watermarks.append(_watermark(session, cid, entity_scope.entity_type, gran,
                                         sources=source_filter))
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
                unroutable=unroutable, unmapped=[], mode=mode, plan_cells=0,
            )

    wanted: list[PlanConcept] = []

    for cid in concept_ids:
        concept = concepts.get(cid)
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
                if (cid, fn.id) in suppressed:
                    suppressed_report.append({
                        "concept_id": cid, "code": concept.code,
                        "reason": "permanently failing path",
                        "source": src, "function_id": fn.id, "command": fn.command,
                    })
                    continue
                if enforce_capability:
                    key = (src, fn.command)
                    status = capability_cache.get(key)
                    if status is None:
                        status = check_command(*key)[0]
                        capability_cache[key] = status
                    if status == UNRESOLVABLE:
                        suppressed_report.append({
                            "concept_id": cid, "code": concept.code,
                            "reason": "endpoint unresolvable",
                            "source": src, "function_id": fn.id, "command": fn.command,
                        })
                        continue
                sources.append(PlanSource(
                    source=src, score=cand["score"],
                    function_id=fn.id, function_command=fn.command,
                    column_name=binding.column.name, binding_id=binding.id,
                    confidence=binding.confidence,
                    bulk_snapshot=bool(getattr(fn, "bulk_snapshot", False)),
                ))
                # D6 snapshot-first: a bulk_snapshot function marked below (the
                # subset check needs the unmapped report, computed after the
                # wanted list is built) collapses to ONE cell per date.
                if getattr(fn, "bulk_snapshot", False):
                    snapshot_candidates.add((cid, src))
        if not sources:
            unroutable.append({
                "concept_id": cid, "code": concept.code,
                "reason": ("no bulk_history source for series mode" if mode == "series"
                           else "no confirmed binding / no candidate source"),
            })
            continue
        bound = chain_bound()
        if len(sources) > bound:
            for dropped in sources[bound:]:
                truncated.append({
                    "concept_id": cid, "code": concept.code,
                    "reason": "chain bound exceeded", "bound": bound,
                    "dropped_source": dropped.source,
                    "dropped_function_id": dropped.function_id,
                    "dropped_command": dropped.function_command,
                })
            sources = sources[:bound]
        wanted.append(PlanConcept(
            concept_id=cid, code=concept.code, entity_type=concept.entity_type,
            unit=concept.unit, frequency=concept.frequency,
            granularity=_granularity_for(concept.frequency),
            ranked_sources=sources,
        ))

    unmapped = _identifier_coverage(session, wanted, entity_scope)

    # D6 snapshot-first subset check: the snapshot covers the scope when the
    # scope is "all entities" (the snapshot IS the cross-section) or when every
    # explicit entity has an identifier for that source (no unmapped pair for
    # (source, concept)). Explicit ids partially unmapped -> keep the fan-out.
    unmapped_keys = {(u["source"], u["concept_id"]) for u in unmapped}
    for concept in wanted:
        for ps in concept.ranked_sources:
            if (concept.concept_id, ps.source) in snapshot_candidates:
                ps.bulk_snapshot = (
                    entity_scope.entity_ids is None
                    or (ps.source, concept.concept_id) not in unmapped_keys
                )

    plan = CrawlPlan(
        wanted_concepts=wanted,
        entity_scope=entity_scope,
        date_range=date_range,
        unroutable=unroutable,
        unmapped=unmapped,
        truncated=truncated,
        suppressed=suppressed_report,
        mode=mode,
        plan_cells=_count_cells(session, wanted, entity_scope, date_range, mode),
    )
    return plan


def _count_cells(
    session: Session, wanted: list, entity_scope: EntityScope,
    date_range: DateRange, mode: str,
) -> int:
    """Cells the executor will emit — the no_op/zero_yield discriminator (D3).

    Mirrors the executor's expansion: per_date = entities x dates, series =
    entities, and a bulk_snapshot source collapses to ONE cell per date
    regardless of entity count. For a lazy scope (entity_ids None) the entity
    count is the distinct identifiers for the ranked sources — the same
    estimate the guardrail uses; exact scopes are exact.
    """
    if not wanted:
        return 0
    if entity_scope.entity_ids:
        n_entities = len(entity_scope.entity_ids)
    else:
        sources = {ps.source for pc in wanted for ps in pc.ranked_sources}
        if not sources:
            return 0
        from fd_open_data_mcp.models import EntitySourceIdentifier
        n_entities = (
            session.query(func.count(func.distinct(EntitySourceIdentifier.entity_id)))
            .filter(EntitySourceIdentifier.entity_type == entity_scope.entity_type,
                    EntitySourceIdentifier.source.in_(sources))
            .scalar()
        ) or 0
    total = 0
    for pc in wanted:
        if any(ps.bulk_snapshot for ps in pc.ranked_sources):
            # D6: one snapshot cell per date, independent of entity count
            total += _date_count(date_range.start, date_range.end,
                                 pc.frequency or date_range.frequency) \
                if mode != "series" and date_range.start is not None else 1
            continue
        if mode == "series" or date_range.start is None:
            n_dates = 1
        else:
            n_dates = _date_count(date_range.start, date_range.end,
                                  pc.frequency or date_range.frequency)
        total += n_entities * n_dates
    return total


def _date_count(start: str, end: str, frequency: str | None) -> int:
    """Number of fetch dates in [start, end] for a cadence (mirrors the spider's
    ``_expand_dates``): yearly -> one per year, monthly -> one per month, else daily."""
    s = dt.date.fromisoformat(start[:10])
    e = dt.date.fromisoformat(end[:10])
    if e < s:
        s, e = e, s
    if frequency == "yearly":
        return e.year - s.year + 1
    if frequency == "monthly":
        return (e.year - s.year) * 12 + (e.month - s.month) + 1
    return (e - s).days + 1


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


def _granularity_for(frequency: str | None) -> str:
    """Concept frequency -> observation granularity tag (day|month|year).

    Mirrors the crawler's _granularity; a concept's since-last watermark must advance
    from observations of its OWN cadence (monthly from monthly, not from daily).
    """
    if frequency == "yearly":
        return "year"
    if frequency == "monthly":
        return "month"
    return "day"


def _watermark(session: Session, concept_id: int, entity_type: str,
               granularity: str = "day", sources: list[str] | None = None) -> str | None:
    """Return max(date) for this concept at a given granularity, or None.

    Observations are keyed per (point, source) (add-multi-source-observations), so
    the unfiltered watermark — max over every source's rows — means "the point is
    covered when ANY source holds it"; that stays the default for unfiltered plans.
    A ``sources`` list narrows the scan (a plan's ``source_filter``): a source-scoped
    plan advances from what those sources themselves hold. Filtering by granularity
    keeps a concept's since-last watermark on its own cadence — legacy bare
    'YYYY'/'YYYY-MM' rows (tagged 'day' by the migration heuristic) never corrupt a
    monthly/yearly watermark.
    """
    sql = ("SELECT max(date) FROM semantic_observations "
           "WHERE concept_id=:c AND entity_type=:et AND granularity=:g")
    params: dict = {"c": concept_id, "et": entity_type, "g": granularity}
    stmt = text(sql)
    if sources:
        # expanding bindparam renders the list as an IN (...) tuple
        stmt = text(sql + " AND source_used IN :sources").bindparams(
            bindparam("sources", expanding=True))
        params["sources"] = list(sources)
    row = session.execute(stmt, params).first()
    return row[0] if row and row[0] else None


def _next_period_start(watermark_date_str: str, frequency: str | None) -> str:
    """Advance the watermark date by one period (yearly/monthly/daily).

    Canonical-only: the watermark is always 'YYYY-MM-DD'. Returns the ISO start of the
    next period (the executor expands it to the observation date: yearly -> 12-31,
    monthly -> 01).
    """
    wm = dt.date.fromisoformat(watermark_date_str[:10])

    # Advance by frequency
    if frequency == "yearly":
        nxt = dt.date(wm.year + 1, 1, 1)
    elif frequency == "monthly":
        nxt = (wm.replace(day=1) + dt.timedelta(days=32)).replace(day=1)
    else:  # daily or None
        nxt = wm + dt.timedelta(days=1)

    return nxt.isoformat()

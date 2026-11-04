"""Plan → work-item splitting (add-data-stream-engine task 4.3).

The guardrail (POLICY_MAX_FETCHES) used to REFUSE an over-size plan. Under the
leasing model it instead SHARDS the plan into work items, each small enough to
lease and execute independently, so a large backfill wave becomes many bounded
units rather than one rejected whole.

Splitting is by ENTITY: a plan's work is concepts × entities × dates, and the
entity axis is the one that partitions cleanly without changing what any single
cell means (a cell is still concept×entity×date). Each work item carries the
FULL concept set and date range but a disjoint slice of the entity universe;
the union of the slices is exactly the original scope — no overlap, no gap
(the spec's「工作项之间不重叠、合并不遗漏」).

Two scope shapes:
  - explicit ``entity_ids`` → chunk the list directly.
  - ``None`` (all of a type) → resolve the fetchable universe from the
    identifier table (the same set ``estimate_fetches`` counts), then chunk.
    A scope with no resolvable entities yields NO items (an empty plan is not
    a bug, it is nothing to do).

``force`` semantics are preserved at the CALLER (the reconciler): a force plan
may exceed the per-item cap and is still split; a non-force over-cap plan is
split instead of refused only when the policy opts into leasing.
"""
from __future__ import annotations

from sqlalchemy import func
from sqlalchemy.orm import Session

from fd_open_data_mcp.crawl.plan import CrawlPlan, EntityScope
from fd_open_data_mcp.models import EntitySourceIdentifier


def resolve_entity_ids(session: Session, plan: CrawlPlan) -> list[int]:
    """The fetchable entity ids for a plan's scope, sorted.

    Explicit ids are returned as-is (sorted for determinism). A filter scope
    (entity_ids None) resolves the entities that carry an identifier for at
    least one ranked source — the same universe the estimate counts, so the
    split covers exactly the fetchable work.
    """
    scope = plan.entity_scope
    if scope.entity_ids is not None:
        return sorted(set(scope.entity_ids))
    sources = {rs.source for pc in plan.wanted_concepts for rs in pc.ranked_sources}
    if not sources:
        return []
    rows = (
        session.query(func.distinct(EntitySourceIdentifier.entity_id))
        .filter(EntitySourceIdentifier.entity_type == scope.entity_type,
                EntitySourceIdentifier.source.in_(sources))
        .all()
    )
    return sorted(r[0] for r in rows)


def chunk_ids(ids: list[int], max_per_item: int) -> list[list[int]]:
    """Partition ``ids`` into chunks of at most ``max_per_item``, order kept.

    Guarantees: chunks are disjoint, their union == the input set, and no
    chunk exceeds the cap. Empty input → no chunks.
    """
    if max_per_item < 1:
        raise ValueError("max_per_item must be >= 1")
    return [ids[i:i + max_per_item] for i in range(0, len(ids), max_per_item)]


def _estimate_per_entity(plan: CrawlPlan) -> int:
    """Fetch cells per entity for this plan (dates × concepts, series=1 date)."""
    from fd_open_data_mcp.refresh.reconciler import _date_count
    if plan.mode == "series":
        n_dates = 1
    else:
        n_dates = 1
        # per-date: the widest concept's date count drives the cap
        if plan.date_range.start is not None:
            n_dates = max(
                _date_count(plan.date_range.start, plan.date_range.end,
                            pc.frequency or plan.date_range.frequency)
                for pc in plan.wanted_concepts) or 1
    return max(1, n_dates * len(plan.wanted_concepts))


def split_plan(session: Session, plan: CrawlPlan, *, max_cells_per_item: int
               ) -> list[CrawlPlan]:
    """Split a plan into work-item sub-plans, each ≤ ``max_cells_per_item``.

    Returns sub-plans carrying the full concept set + date range and a disjoint
    entity slice. The per-entity cell estimate sets the entity chunk size so
    each item stays under the cap. A plan with no fetchable entities yields an
    empty list (nothing to do, not a refusal).
    """
    ids = resolve_entity_ids(session, plan)
    if not ids:
        return []
    per_entity = _estimate_per_entity(plan)
    chunk = max(1, max_cells_per_item // per_entity)
    items: list[CrawlPlan] = []
    for group in chunk_ids(ids, chunk):
        sub = plan.model_copy(deep=True)
        sub.entity_scope = EntityScope(
            entity_type=plan.entity_scope.entity_type,
            entity_ids=group, filter=None)
        # plan_cells recomputed for the slice (the executor's own count)
        sub.plan_cells = len(group) * per_entity if plan.mode != "series" else len(group)
        items.append(sub)
    return items


def verify_partition(original_ids: list[int], items: list[CrawlPlan]) -> bool:
    """Assertion helper for tests/callers: the items partition the original
    scope exactly — disjoint and covering."""
    seen: list[int] = []
    for it in items:
        seen.extend(it.entity_scope.entity_ids or [])
    if len(seen) != len(set(seen)):
        return False  # overlap
    return set(seen) == set(original_ids)

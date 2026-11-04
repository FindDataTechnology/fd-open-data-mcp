"""Plan splitting into work items (task 4.3): disjoint, covering, capped."""
from __future__ import annotations

import pytest

from fd_open_data_mcp.crawl.plan import (
    CrawlPlan, DateRange, EntityScope, PlanConcept, PlanSource,
)
from fd_open_data_mcp.crawl.split import (
    chunk_ids, resolve_entity_ids, split_plan, verify_partition,
)
from fd_open_data_mcp.models import Concept, Entity, EntitySourceIdentifier


def _plan(entity_ids=None, n_concepts=2, mode="per_date",
          start="2024-01-01", end="2024-12-31"):
    concepts = []
    for i in range(n_concepts):
        concepts.append(PlanConcept(
            concept_id=i + 1, code=f"c{i}", entity_type="stock",
            frequency="daily", granularity="day",
            ranked_sources=[PlanSource(
                source="s", score=0.9, function_id=1, function_command="f",
                column_name="v", binding_id=1, confidence=0.9)]))
    return CrawlPlan(
        mode=mode, wanted_concepts=concepts,
        entity_scope=EntityScope(entity_type="stock", entity_ids=entity_ids),
        date_range=DateRange(start=start, end=end, frequency="daily"))


def test_chunk_ids_disjoint_covering():
    ids = list(range(10))
    chunks = chunk_ids(ids, 3)
    assert chunks == [[0, 1, 2], [3, 4, 5], [6, 7, 8], [9]]
    flat = [x for c in chunks for x in c]
    assert flat == ids  # order kept, no dup, no gap


def test_chunk_empty():
    assert chunk_ids([], 5) == []
    with pytest.raises(ValueError):
        chunk_ids([1], 0)


def test_split_explicit_ids_partitions(session):
    plan = _plan(entity_ids=list(range(1, 11)))
    items = split_plan(session, plan, max_cells_per_item=20)
    assert len(items) > 1
    assert verify_partition(list(range(1, 11)), items)


def test_split_each_item_under_cap(session):
    # 10-day range × 2 concepts → 20 cells/entity; cap 50 → chunks of 2 entities
    plan = _plan(entity_ids=list(range(1, 101)), start="2024-01-01", end="2024-01-10")
    items = split_plan(session, plan, max_cells_per_item=50)
    assert len(items) > 1
    for it in items:
        assert it.plan_cells <= 50
    assert verify_partition(list(range(1, 101)), items)


def test_indivisible_unit_exceeds_cap_still_splits_by_entity(session):
    """When one entity's work alone exceeds the cap (365 days × 2 concepts),
    entity splitting can't go below it — each item is one entity, the minimal
    indivisible unit. The whole plan is NOT refused (spec 4.3)."""
    plan = _plan(entity_ids=list(range(1, 6)))  # full-year daily
    items = split_plan(session, plan, max_cells_per_item=50)
    assert len(items) == 5                      # one per entity (chunk floors to 1)
    assert verify_partition(list(range(1, 6)), items)


def test_split_filter_scope_resolves_from_identifiers(session):
    # 5 entities carrying an identifier for source "s" (the identifier table
    # holds a logical FK, so no Entity row is required)
    for i in range(1, 6):
        session.add(EntitySourceIdentifier(
            entity_id=i, entity_type="stock", source="s", identifier=f"s{i}"))
    session.commit()
    plan = _plan(entity_ids=None)
    ids = resolve_entity_ids(session, plan)
    assert ids == [1, 2, 3, 4, 5]
    items = split_plan(session, plan, max_cells_per_item=10)
    assert verify_partition(ids, items)


def test_split_no_entities_yields_nothing(session):
    plan = _plan(entity_ids=None)  # no identifiers seeded
    assert split_plan(session, plan, max_cells_per_item=10) == []


def test_split_preserves_concepts_and_dates(session):
    plan = _plan(entity_ids=list(range(1, 7)))
    items = split_plan(session, plan, max_cells_per_item=4)
    for it in items:
        assert len(it.wanted_concepts) == len(plan.wanted_concepts)
        assert it.date_range.start == plan.date_range.start
        assert it.date_range.end == plan.date_range.end


def test_series_mode_split(session):
    plan = _plan(entity_ids=list(range(1, 11)), mode="series")
    items = split_plan(session, plan, max_cells_per_item=3)
    assert verify_partition(list(range(1, 11)), items)
    for it in items:
        assert it.plan_cells == len(it.entity_scope.entity_ids)  # series: 1 per entity

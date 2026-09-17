"""Bounded failover chain (spec concept-fetch).

A concept's chain is built from every binding for it, in rank order, with no
cap. Concept `price.close`/`stock` reached **117** dispatch-eligible bindings,
and each candidate costs up to `max_proxies x per-proxy retries` upstream calls
— so one cell could issue several hundred requests before giving up. Candidates
past the bound are recorded as not-attempted, never dropped silently.
"""
from __future__ import annotations

import pytest

from fd_open_data_mcp.crawl.plan import DateRange, EntityScope
from fd_open_data_mcp.crawl.planner import chain_bound, plan_crawl
from fd_open_data_mcp.models import (
    Concept, ConceptBinding, Function, FunctionColumn, Source,
)


def _seed_concept_with_candidates(session, n: int) -> Concept:
    """One stock concept with ``n`` confirmed, in-domain candidates."""
    source = Source(name="akshare", label="akshare")
    session.add(source)
    session.flush()
    concept = Concept(code="price.close", entity_type="stock", measure="",
                      unit="currency", frequency="daily")
    session.add(concept)
    session.flush()
    for i in range(n):
        fn = Function(source_id=source.id, command=f"stock_zh_a_hist_{i}")
        session.add(fn)
        session.flush()
        col = FunctionColumn(function_id=fn.id, name=f"收盘{i}")
        session.add(col)
        session.flush()
        session.add(ConceptBinding(
            concept_id=concept.id, column_id=col.id, confidence=0.9,
            provenance="manual", reviewed=True))
    session.commit()
    return concept


def _plan_for(session, concept_id):
    return plan_crawl(
        session, [concept_id],
        EntityScope(entity_type="stock", entity_ids=[1]),
        DateRange(start="2024-07-01", end="2024-07-02", frequency="daily"),
    )


DEFAULT_BOUND = 8


def test_the_default_bound_is_single_digit():
    assert chain_bound() == DEFAULT_BOUND


def test_the_bound_is_configurable(monkeypatch):
    monkeypatch.setenv("FD_CRAWL_CHAIN_MAX", "3")
    assert chain_bound() == 3


def test_a_malformed_bound_falls_back_to_the_default(monkeypatch):
    """A bad env value must not disable the cap."""
    monkeypatch.setenv("FD_CRAWL_CHAIN_MAX", "lots")
    assert chain_bound() == DEFAULT_BOUND


def test_a_concept_with_many_candidates_yields_a_bounded_chain(session):
    """The regression this exists for: 117 candidates, one cell, hundreds of calls."""
    concept = _seed_concept_with_candidates(session, 40)
    plan = _plan_for(session, concept.id)

    assert len(plan.wanted_concepts) == 1
    assert len(plan.wanted_concepts[0].ranked_sources) == DEFAULT_BOUND


def test_truncated_candidates_are_reported_not_dropped(session):
    concept = _seed_concept_with_candidates(session, 40)
    plan = _plan_for(session, concept.id)

    assert len(plan.truncated) == 40 - DEFAULT_BOUND
    assert {t["concept_id"] for t in plan.truncated} == {concept.id}
    assert all(t["reason"] == "chain bound exceeded" for t in plan.truncated)
    assert all(t["bound"] == DEFAULT_BOUND for t in plan.truncated)
    # the report identifies what was not attempted, not just how many
    assert all(t["dropped_source"] and t["dropped_command"] for t in plan.truncated)


def test_a_concept_within_the_bound_is_untouched(session):
    concept = _seed_concept_with_candidates(session, 3)
    plan = _plan_for(session, concept.id)

    assert len(plan.wanted_concepts[0].ranked_sources) == 3
    assert plan.truncated == []


def test_the_bound_reaches_the_serialised_plan(session):
    """The plan file is the run's evidence — the truncation must survive into it."""
    concept = _seed_concept_with_candidates(session, 20)
    plan = _plan_for(session, concept.id)

    dumped = plan.model_dump(mode="json")
    assert len(dumped["truncated"]) == 20 - DEFAULT_BOUND
    assert len(dumped["wanted_concepts"][0]["ranked_sources"]) == DEFAULT_BOUND


def test_a_smaller_bound_truncates_more(session, monkeypatch):
    monkeypatch.setenv("FD_CRAWL_CHAIN_MAX", "2")
    concept = _seed_concept_with_candidates(session, 10)
    plan = _plan_for(session, concept.id)

    assert len(plan.wanted_concepts[0].ranked_sources) == 2
    assert len(plan.truncated) == 8

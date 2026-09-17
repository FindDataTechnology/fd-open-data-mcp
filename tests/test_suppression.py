"""Suppression of permanently-failing (concept, function) paths (spec concept-fetch).

A permanent failure is intrinsic to the request — the library has no such
callable — so it is missing from EVERY egress. That is why a permanent path is
*excluded* rather than demoted (reordering to last still means attempted), and
why suppression needs no cluster scope where demotion does.

The evidence is `fetch_log`, so nothing is hand-maintained and nothing is
deleted: an ordinary outcome, or an operator's marker row, breaks the run and
restores the path.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from fd_open_data_mcp.crawl.plan import DateRange, EntityScope
from fd_open_data_mcp.crawl.planner import plan_crawl
from fd_open_data_mcp.fetch import suppress
from fd_open_data_mcp.models import (
    Concept, ConceptBinding, FetchLog, Function, FunctionColumn, Source,
)


def _log(session, concept_id, function_id, classification, status="error"):
    session.add(FetchLog(
        source="akshare", concept_id=concept_id, function_id=function_id,
        status=status, classification=classification, latency_ms=1,
        timestamp=datetime.now(timezone.utc)))
    session.commit()


def _seed(session, n: int):
    """One stock concept with n confirmed candidates; returns (concept, fids)."""
    source = Source(name="akshare", label="akshare")
    session.add(source)
    session.flush()
    concept = Concept(code="price.close", entity_type="stock", measure="",
                      unit="currency", frequency="daily")
    session.add(concept)
    session.flush()
    fids = []
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
        fids.append(fn.id)
    session.commit()
    return concept, fids


def _plan(session, concept_id):
    return plan_crawl(session, [concept_id],
                      EntityScope(entity_type="stock", entity_ids=[1]),
                      DateRange(start="2024-07-01", end="2024-07-02", frequency="daily"))


# --- 7.1 derivation ----------------------------------------------------------

def test_all_permanent_outcomes_suppress_the_pair(session):
    concept, fids = _seed(session, 1)
    for _ in range(3):
        _log(session, concept.id, fids[0], suppress.PERMANENT)

    assert suppress.suppressed_paths(session, {(concept.id, fids[0])}) == {
        (concept.id, fids[0])}


def test_fewer_than_the_threshold_does_not_suppress(session):
    concept, fids = _seed(session, 1)
    _log(session, concept.id, fids[0], suppress.PERMANENT)

    assert suppress.suppressed_paths(session, {(concept.id, fids[0])}) == set()


def test_the_threshold_is_configurable(session, monkeypatch):
    monkeypatch.setenv(suppress.SUPPRESS_MIN_ENV, "1")
    concept, fids = _seed(session, 1)
    _log(session, concept.id, fids[0], suppress.PERMANENT)

    assert suppress.suppressed_paths(session, {(concept.id, fids[0])}) == {
        (concept.id, fids[0])}


def test_a_malformed_threshold_falls_back_to_the_default(session, monkeypatch):
    monkeypatch.setenv(suppress.SUPPRESS_MIN_ENV, "many")
    assert suppress.suppress_threshold() == suppress.DEFAULT_SUPPRESS_MIN


def test_only_the_recent_run_counts(session):
    """An old permanent streak followed by success is not a suppressed path."""
    concept, fids = _seed(session, 1)
    for _ in range(3):
        _log(session, concept.id, fids[0], suppress.PERMANENT)
    _log(session, concept.id, fids[0], "ok", status="ok")

    assert suppress.suppressed_paths(session, {(concept.id, fids[0])}) == set()


def test_unrelated_pairs_are_untouched(session):
    concept, fids = _seed(session, 2)
    for _ in range(3):
        _log(session, concept.id, fids[0], suppress.PERMANENT)

    assert suppress.suppressed_paths(session, {(concept.id, fids[1])}) == set()


# --- 7.3 a transient failure is unaffected ----------------------------------

def test_a_transient_failure_is_never_suppressed(session):
    """Transience is route health: it is demoted and may recover, never
    excluded. Suppressing it would remove a path that can heal."""
    concept, fids = _seed(session, 1)
    for _ in range(10):
        _log(session, concept.id, fids[0], "transient")
    for _ in range(10):
        _log(session, concept.id, fids[0], "ban")

    assert suppress.suppressed_paths(session, {(concept.id, fids[0])}) == set()


def test_a_transient_outcome_breaks_a_permanent_run(session):
    concept, fids = _seed(session, 1)
    for _ in range(3):
        _log(session, concept.id, fids[0], suppress.PERMANENT)
    _log(session, concept.id, fids[0], "transient")

    assert suppress.suppressed_paths(session, {(concept.id, fids[0])}) == set()


# --- 7.4 clearing ------------------------------------------------------------

def test_clearing_the_suppression_restores_the_path(session):
    concept, fids = _seed(session, 1)
    for _ in range(3):
        _log(session, concept.id, fids[0], suppress.PERMANENT)
    assert suppress.suppressed_paths(session, {(concept.id, fids[0])}) != set()

    assert suppress.clear_suppression(session, concept.id, fids[0]) is True

    assert suppress.suppressed_paths(session, {(concept.id, fids[0])}) == set()


def test_clearing_is_recorded_not_silent(session):
    """The clearing is auditable and never deletes the evidence that produced
    the suppression."""
    concept, fids = _seed(session, 1)
    for _ in range(3):
        _log(session, concept.id, fids[0], suppress.PERMANENT)
    suppress.clear_suppression(session, concept.id, fids[0])

    marker = (session.query(FetchLog)
              .filter_by(concept_id=concept.id, function_id=fids[0],
                         classification=suppress.CLEARED)
              .one())
    assert "cleared by operator" in marker.detail
    # the permanent rows are still there
    assert session.query(FetchLog).filter_by(
        classification=suppress.PERMANENT).count() == 3


# --- the report --------------------------------------------------------------

def test_report_lists_suppressed_paths_with_evidence(session):
    concept, fids = _seed(session, 2)
    for _ in range(4):
        _log(session, concept.id, fids[0], suppress.PERMANENT)
    for _ in range(3):
        _log(session, concept.id, fids[1], "transient")

    result = suppress.report(session)
    ids = {(r["concept_id"], r["function_id"]) for r in result["suppressed"]}
    assert ids == {(concept.id, fids[0])}
    assert result["suppressed"][0]["permanent_outcomes"] == 4
    assert result["threshold"] == suppress.DEFAULT_SUPPRESS_MIN


# --- 7.2 applied at plan time ------------------------------------------------

def test_the_planner_excludes_a_suppressed_path(session):
    concept, fids = _seed(session, 3)
    for _ in range(3):
        _log(session, concept.id, fids[0], suppress.PERMANENT)

    plan = _plan(session, concept.id)
    chosen = [ps.function_id for ps in plan.wanted_concepts[0].ranked_sources]
    assert fids[0] not in chosen
    assert len(chosen) == 2


def test_the_planner_reports_what_it_excluded(session):
    concept, fids = _seed(session, 3)
    for _ in range(3):
        _log(session, concept.id, fids[0], suppress.PERMANENT)

    plan = _plan(session, concept.id)
    assert [s["function_id"] for s in plan.suppressed] == [fids[0]]
    assert plan.suppressed[0]["reason"] == "permanently failing path"
    assert plan.suppressed[0]["concept_id"] == concept.id


def test_suppression_is_applied_before_the_chain_bound(session, monkeypatch):
    """Otherwise a 117-candidate chain could be truncated to a handful of
    candidates that are all impossible, while viable ones were discarded."""
    from fd_open_data_mcp.crawl import planner

    monkeypatch.setenv("FD_CRAWL_CHAIN_MAX", "2")
    concept, fids = _seed(session, 5)
    # suppress the two highest-ranked candidates
    for fid in fids[:2]:
        for _ in range(3):
            _log(session, concept.id, fid, suppress.PERMANENT)

    plan = _plan(session, concept.id)
    chosen = [ps.function_id for ps in plan.wanted_concepts[0].ranked_sources]
    assert len(chosen) == 2
    assert not (set(chosen) & set(fids[:2]))    # the bound was filled with viable ones
    assert planner.chain_bound() == 2


def test_a_plan_with_no_suppression_is_unchanged(session):
    concept, fids = _seed(session, 3)
    plan = _plan(session, concept.id)

    assert len(plan.wanted_concepts[0].ranked_sources) == 3
    assert plan.suppressed == []

"""Ingester e2e (task 3.3): event path == direct path, idempotent, multi-grain.

The verification the spec pins:
  - an event and a direct write of the same point produce identical rows +
    rollup state (both land via write_cache_range)
  - at-least-once redelivery never duplicates (natural-key upsert + insert-only
    rollup counting)
  - multi-grain events (quarter, the 64% of production) land with the right
    granularity — the write path's granularity threading (3.3 prerequisite)
"""
from __future__ import annotations

import pytest

from fd_open_data_mcp.fetch.cache import write_cache, write_cache_range
from fd_open_data_mcp.models import (
    Concept, ConceptCoverage, ConceptSourceStats, SemanticObservation,
)
from fd_open_data_mcp.stream.envelope import EventEnvelope, new_event_id
from fd_open_data_mcp.stream.ingest import ingest_batch, run_ingest_loop
from fd_open_data_mcp.stream.memory import InMemoryStream


@pytest.fixture
def concept(session):
    c = Concept(code="gdp", entity_type="country", measure="gdp",
                unit="CNY", frequency="yearly")
    session.add(c)
    session.commit()
    return c


def _ev(cid, eid, date, gran="day", src="s", value="1.0"):
    return EventEnvelope(event_id=new_event_id(), source="t", concept_id=cid,
                         entity_type="country", entity_id=eid, date=date,
                         granularity=gran, source_used=src, value=value, unit="CNY")


def _rows(session, cid):
    return {(r.date, r.granularity, r.source_used, r.value)
            for r in session.query(SemanticObservation).filter_by(concept_id=cid)}


def _cov(session, cid):
    c = session.query(ConceptCoverage).filter_by(concept_id=cid).one()
    return (c.rows, c.coexisting_points, c.sources, c.latest_date)


def test_event_path_equals_direct_path(session, concept):
    """Same three points, one landed direct, one via events → identical."""
    # direct
    write_cache_range(session, concept.id, "country", 1,
                      {"2022-12-31": "1", "2023-12-31": "2", "2024-12-31": "3"},
                      "CNY", "s", granularity="year")
    direct_rows = _rows(session, concept.id)
    direct_cov = _cov(session, concept.id)
    # reset — clear the rollup tables too (the hook is incremental; stale
    # stats would double-count the second landing)
    session.query(SemanticObservation).delete()
    session.query(ConceptCoverage).delete()
    session.query(ConceptSourceStats).delete()
    session.commit()

    # event
    stream = InMemoryStream("t")
    stream.publish([_ev(concept.id, 1, d, "year", "s", v) for d, v in
                    [("2022-12-31", "1"), ("2023-12-31", "2"), ("2024-12-31", "3")]])
    stream.ensure_group("g")
    ingest_batch(session, stream, group="g", consumer="c1")

    assert _rows(session, concept.id) == direct_rows
    assert _cov(session, concept.id) == direct_cov


def test_redelivery_is_idempotent(session, concept):
    stream = InMemoryStream("t")
    evs = [_ev(concept.id, 1, "2024-12-31", "year", "s", "7")]
    stream.publish(evs)
    stream.ensure_group("g")
    ingest_batch(session, stream, group="g", consumer="c1")
    first_rows = _rows(session, concept.id)
    first_cov = _cov(session, concept.id)

    # re-publish the SAME event_id (simulates redelivery after a crash)
    stream.publish(evs)
    ingest_batch(session, stream, group="g", consumer="c1")
    # same point, same source → upsert update, not a new row; rollup rows count
    # unchanged (the second land is an update, not an insert)
    assert _rows(session, concept.id) == first_rows
    assert _cov(session, concept.id)[0] == first_cov[0]  # rows count stable


def test_multi_grain_lands_correctly(session, concept):
    """quarter (64% of prod) and day coexist as distinct points."""
    stream = InMemoryStream("t")
    stream.publish([
        _ev(concept.id, 1, "2024-12-31", "quarter", "s", "q"),
        _ev(concept.id, 1, "2024-12-31", "day", "s", "d"),
    ])
    stream.ensure_group("g")
    ingest_batch(session, stream, group="g", consumer="c1")
    grains = {r.granularity for r in
              session.query(SemanticObservation).filter_by(concept_id=concept.id)}
    assert grains == {"quarter", "day"}
    cov = _cov(session, concept.id)
    assert cov[0] == 2  # two distinct points


def test_coexisting_sources_across_paths(session, concept):
    """A direct write then an event for the same point under another source →
    coexisting_points=1, rows stays 1 (one point)."""
    write_cache(session, concept.id, "country", 1, "2024-12-31", "1", "CNY",
                "src_a", granularity="year")
    stream = InMemoryStream("t")
    stream.publish([_ev(concept.id, 1, "2024-12-31", "year", "src_b", "2")])
    stream.ensure_group("g")
    ingest_batch(session, stream, group="g", consumer="c1")
    cov = _cov(session, concept.id)
    assert cov[0] == 1          # one point
    assert cov[1] == 1          # coexisting
    assert cov[2] == 2          # two sources


def test_run_ingest_loop_drains(session, concept):
    stream = InMemoryStream("t")
    stream.publish([_ev(concept.id, i, "2024-12-31", "year", "s")
                    for i in range(1, 6)])
    stream.ensure_group("g")
    total = run_ingest_loop(session, stream, group="g", consumer="c1")
    assert total == 5
    assert session.query(SemanticObservation).filter_by(concept_id=concept.id).count() == 5

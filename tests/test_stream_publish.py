"""Worker-exit publish primitive (task 3.4): event / direct / degraded modes.

Pins the degradation contract: a sink outage falls back to a direct write with
NO data loss and NO duplicate semantics (both paths land through the same
write_cache_range), and the returned mode lets the caller mark the run detail.
"""
from __future__ import annotations

import pytest

from fd_open_data_mcp.models import Concept, SemanticObservation
from fd_open_data_mcp.stream.memory import InMemoryStream
from fd_open_data_mcp.stream.publish import emit_observations


@pytest.fixture
def concept(session):
    c = Concept(code="gdp", entity_type="country", measure="gdp",
                unit="CNY", frequency="yearly")
    session.add(c)
    session.commit()
    return c


def _recs(cid, n=3):
    return [{"concept_id": cid, "entity_type": "country", "entity_id": 1,
             "date": f"202{i}-12-31", "granularity": "year",
             "source_used": "s", "value": str(i), "unit": "CNY", "source": "t"}
            for i in range(n)]


def test_no_sink_writes_direct(session, concept):
    out = emit_observations(session, None, _recs(concept.id))
    assert out["mode"] == "direct" and out["rows"] == 3
    assert session.query(SemanticObservation).filter_by(concept_id=concept.id).count() == 3


def test_sink_configured_publishes_event(session, concept):
    sink = InMemoryStream("t")
    out = emit_observations(session, sink, _recs(concept.id))
    assert out["mode"] == "event" and out["published"] == 3
    # nothing landed yet (the ingester lands it) — the publish path writes no row
    assert session.query(SemanticObservation).filter_by(concept_id=concept.id).count() == 0


def test_publish_failure_degrades_to_direct_no_loss(session, concept):
    class BoomSink(InMemoryStream):
        def publish(self, events):
            raise RuntimeError("redis down")

    out = emit_observations(session, BoomSink("t"), _recs(concept.id))
    assert out["mode"] == "degraded" and out["rows"] == 3
    assert "redis down" in out["error"]
    # data landed via the direct fallback — no loss
    assert session.query(SemanticObservation).filter_by(concept_id=concept.id).count() == 3


def test_degrade_disabled_raises(session, concept):
    class BoomSink(InMemoryStream):
        def publish(self, events):
            raise RuntimeError("redis down")

    with pytest.raises(RuntimeError, match="redis down"):
        emit_observations(session, BoomSink("t"), _recs(concept.id),
                          degrade_to_direct=False)


def test_degraded_write_matches_event_path(session, concept):
    """The degraded direct write and an event landing produce identical rows
    (transport differs, data semantics never)."""
    from fd_open_data_mcp.stream.ingest import ingest_batch

    class BoomSink(InMemoryStream):
        def publish(self, events):
            raise RuntimeError("down")

    emit_observations(session, BoomSink("t"), _recs(concept.id))
    degraded = {(r.date, r.value, r.granularity)
                for r in session.query(SemanticObservation).filter_by(concept_id=concept.id)}

    # event path on a fresh concept
    c2 = Concept(code="gdp2", entity_type="country", measure="gdp",
                 unit="CNY", frequency="yearly")
    session.add(c2); session.commit()
    sink = InMemoryStream("t2")
    emit_observations(session, sink, _recs(c2.id))
    sink.ensure_group("g")
    ingest_batch(session, sink, group="g", consumer="c1")
    evented = {(r.date, r.value, r.granularity)
               for r in session.query(SemanticObservation).filter_by(concept_id=c2.id)}
    assert degraded == evented


def test_empty_records_noop(session, concept):
    assert emit_observations(session, None, []) == {"mode": "noop", "rows": 0}

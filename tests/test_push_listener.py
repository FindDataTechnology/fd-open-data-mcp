"""Realtime push channel (task 4.5): push → event → idempotent land.

Pins: a pushed record lands via the stream plane (seconds, not the 5-min cron);
a duplicate push of the same point is an update not a second row (natural-key
idempotency, so outage-recovery has no dup/gap); a malformed payload is a loud
error; a sink outage degrades to a direct write.
"""
from __future__ import annotations

import pytest

from fd_open_data_mcp.models import Concept, SemanticObservation
from fd_open_data_mcp.push.listener import handle_push, handle_single
from fd_open_data_mcp.stream.ingest import ingest_batch
from fd_open_data_mcp.stream.memory import InMemoryStream


@pytest.fixture
def concept(session):
    c = Concept(code="price.close", entity_type="stock", measure="close",
                unit="CNY", frequency="realtime")
    session.add(c)
    session.commit()
    return c


def _rec(cid, date="2024-06-01", value="1.0"):
    return {"concept_id": cid, "entity_type": "stock", "entity_id": 1,
            "date": date, "value": value, "unit": "CNY", "source_used": "push"}


def _rows(session, cid):
    return session.query(SemanticObservation).filter_by(concept_id=cid).count()


def test_push_publishes_event(session, concept):
    sink = InMemoryStream("t")
    out = handle_single(session, sink, "feed", _rec(concept.id))
    assert out["mode"] == "event" and out["published"] == 1
    # not landed yet (the ingester lands it) — push is the transport
    assert _rows(session, concept.id) == 0


def test_push_then_ingest_lands(session, concept):
    sink = InMemoryStream("t")
    handle_push(session, sink, "feed", [_rec(concept.id, "2024-06-01"),
                                        _rec(concept.id, "2024-06-02")])
    sink.ensure_group("g")
    ingest_batch(session, sink, group="g", consumer="c1")
    assert _rows(session, concept.id) == 2


def test_duplicate_push_lands_once(session, concept):
    """Two pushes of the SAME point → one row (natural-key dedup, no ledger)."""
    sink = InMemoryStream("t")
    handle_single(session, sink, "feed", _rec(concept.id, "2024-06-01", "1"))
    handle_single(session, sink, "feed", _rec(concept.id, "2024-06-01", "2"))
    sink.ensure_group("g")
    ingest_batch(session, sink, group="g", consumer="c1")
    assert _rows(session, concept.id) == 1
    row = session.query(SemanticObservation).filter_by(concept_id=concept.id).one()
    assert row.value == "2"  # the later push's value wins (an update)


def test_malformed_push_raises(session, concept):
    sink = InMemoryStream("t")
    with pytest.raises(ValueError, match="missing field"):
        handle_single(session, sink, "feed", {"entity_id": 1})  # no concept_id


def test_push_degrades_on_sink_failure(session, concept):
    class BoomSink(InMemoryStream):
        def publish(self, events):
            raise RuntimeError("redis down")

    out = handle_single(session, BoomSink("t"), "feed", _rec(concept.id))
    assert out["mode"] == "degraded" and out["rows"] == 1
    assert _rows(session, concept.id) == 1  # landed via the direct fallback


def test_push_no_sink_writes_direct(session, concept):
    out = handle_single(session, None, "feed", _rec(concept.id))
    assert out["mode"] == "direct" and out["rows"] == 1

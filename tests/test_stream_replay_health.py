"""Replay tool + stream health (tasks 3.5/3.6).

Replay: re-lands events by time range through the SAME idempotent path as the
consumer (no upstream fetch), is cancellable between chunks, and reports
progress. Health: backlog / oldest-idle / consumers with a threshold alert.
"""
from __future__ import annotations

import time

import pytest

from fd_open_data_mcp.models import Concept, SemanticObservation
from fd_open_data_mcp.stream.envelope import EventEnvelope, new_event_id
from fd_open_data_mcp.stream.health import stream_health
from fd_open_data_mcp.stream.ingest import ingest_batch
from fd_open_data_mcp.stream.memory import InMemoryStream
from fd_open_data_mcp.stream.replay import replay


@pytest.fixture
def concept(session):
    c = Concept(code="gdp", entity_type="country", measure="gdp",
                unit="CNY", frequency="yearly")
    session.add(c)
    session.commit()
    return c


def _ev(cid, eid, date):
    return EventEnvelope(event_id=new_event_id(), source="t", concept_id=cid,
                         entity_type="country", entity_id=eid, date=date,
                         granularity="year", source_used="s", value="1", unit="CNY")


def _rows(session, cid):
    return session.query(SemanticObservation).filter_by(concept_id=cid).count()


def test_replay_relands_idempotently(session, concept):
    stream = InMemoryStream("t")
    stream.publish([_ev(concept.id, i, "2024-12-31") for i in range(1, 4)])
    stream.ensure_group("g")
    ingest_batch(session, stream, group="g", consumer="c1")
    assert _rows(session, concept.id) == 3

    now_ms = int(time.time() * 1000)
    out = replay(session, stream, start_ms=now_ms - 1000, end_ms=now_ms + 10_000)
    assert out["status"] == "done" and out["replayed"] == 3
    # re-landing the same points: no new rows (natural-key upsert)
    assert _rows(session, concept.id) == 3


def test_replay_future_window_lands_nothing(session, concept):
    stream = InMemoryStream("t")
    stream.publish([_ev(concept.id, 1, "2024-12-31")])
    stream.ensure_group("g")
    now_ms = int(time.time() * 1000)
    out = replay(session, stream, start_ms=now_ms - 10_000_000,
                 end_ms=now_ms - 9_000_000)
    assert out["scanned"] == 0 and out["replayed"] == 0


def test_replay_cancellable(session, concept):
    stream = InMemoryStream("t")
    stream.publish([_ev(concept.id, i, f"202{i}-12-31") for i in range(1, 6)])
    stream.ensure_group("g")
    calls = {"n": 0}

    def cancel_after_one():
        calls["n"] += 1
        return calls["n"] > 1  # cancel on the second chunk poll

    out = replay(session, stream, chunk_size=2, should_cancel=cancel_after_one)
    assert out["status"] == "cancelled"
    # first chunk of 2 landed, then cancelled
    assert _rows(session, concept.id) == 2


def test_replay_progress_callback(session, concept):
    stream = InMemoryStream("t")
    stream.publish([_ev(concept.id, i, f"202{i}-12-31") for i in range(1, 5)])
    seen = []
    replay(session, stream, chunk_size=2, progress=lambda p: seen.append(p))
    assert len(seen) == 2
    assert seen[-1]["replayed"] == 4


# --- health (3.6) ---

def test_health_clean_when_all_acked(session, concept):
    stream = InMemoryStream("t")
    stream.publish([_ev(concept.id, 1, "2024-12-31")])
    stream.ensure_group("g")
    ingest_batch(session, stream, group="g", consumer="c1")
    h = stream_health(stream, "g")
    assert h["pending"] == 0
    assert h["alert"] is False


def test_health_alerts_on_backlog(session, concept):
    stream = InMemoryStream("t")
    stream.publish([_ev(concept.id, i, "2024-12-31") for i in range(1, 6)])
    stream.ensure_group("g")
    stream.read("g", "c1", count=5)  # delivered, none acked
    h = stream_health(stream, "g", backlog_alert=3)
    assert h["pending"] == 5
    assert h["alert"] is True
    assert any("backlog" in r for r in h["alert_reasons"])


def test_health_reports_oldest_idle(session, concept):
    stream = InMemoryStream("t")
    stream.publish([_ev(concept.id, 1, "2024-12-31")])
    stream.ensure_group("g")
    stream.read("g", "c1", count=1)
    h = stream_health(stream, "g")
    assert h["oldest_pending_idle_ms"] is not None
    assert h["oldest_pending_idle_ms"] >= 0

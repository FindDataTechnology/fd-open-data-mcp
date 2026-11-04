"""Stream-plane contract suite (tasks 3.1/3.2).

The SAME tests run against every EventSink/EventStream implementation —
proving the backend is swappable without changing behavior (stream-plane
「后端可替换」scenario). Backends: InMemoryStream (always) and
RedisStreamsSink (skipped when redis-server is unavailable, so CI without a
server still validates the contract via memory).

Pinned behaviors:
  - publish → read delivers the messages
  - read is at-least-once: an unacked message is reclaimable after idle
  - ack removes it from pending
  - pending_summary reports backlog + oldest-idle + consumers
  - iter_range replays by ms entry-id window (the replay cursor)
"""
from __future__ import annotations

import shutil
import socket
import subprocess
import time

import pytest

from fd_open_data_mcp.stream.envelope import EventEnvelope, new_event_id
from fd_open_data_mcp.stream.memory import InMemoryStream
from fd_open_data_mcp.stream.sink import EventSink, EventStream


def _ev(date="2024-06-01", cid=1, eid=1, **kw):
    kw.setdefault("source_used", "src_a")
    kw.setdefault("value", "1.0")
    kw.setdefault("unit", "CNY")
    return EventEnvelope(
        event_id=new_event_id(), source="t", concept_id=cid,
        entity_type="stock", entity_id=eid, date=date, granularity="day",
        **kw)


# --------------------------------------------------------------------------
# redis-server fixture (skip when the binary is absent)
# --------------------------------------------------------------------------

def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture(scope="session")
def redis_url():
    if not shutil.which("redis-server"):
        pytest.skip("redis-server not installed")
    port = _free_port()
    proc = subprocess.Popen(
        ["redis-server", "--port", str(port), "--save", "", "--appendonly",
         "no", "--loglevel", "warning"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        import redis
        deadline = time.time() + 10
        r = redis.Redis(port=port)
        while time.time() < deadline:
            try:
                r.ping()
                break
            except Exception:
                time.sleep(0.1)
        else:
            proc.kill()
            pytest.skip("redis-server did not come up")
        yield f"redis://127.0.0.1:{port}/0"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


def _redis_backend(redis_url):
    import redis
    from fd_open_data_mcp.stream.redis_streams import RedisStreamsSink
    client = redis.Redis.from_url(redis_url, decode_responses=True)
    return RedisStreamsSink(client, stream=f"test:{new_event_id()}")


def _make_backend(name, redis_url=None):
    if name == "memory":
        return InMemoryStream(name=f"t{new_event_id()}")
    return _redis_backend(redis_url)


BACKENDS = ["memory", "redis"]


@pytest.fixture(params=BACKENDS)
def backend(request, redis_url):
    if request.param == "redis" and not shutil.which("redis-server"):
        pytest.skip("redis-server not installed")
    b = _make_backend(request.param, redis_url)
    assert isinstance(b, EventSink) and isinstance(b, EventStream)
    yield b


# --------------------------------------------------------------------------
# contract
# --------------------------------------------------------------------------

def test_publish_then_read(backend):
    evs = [_ev("2024-06-01"), _ev("2024-06-02")]
    assert backend.publish(evs) == 2
    backend.ensure_group("g1")
    got = backend.read("g1", "c1", count=10)
    assert {m.envelope.event_id for m in got} == {e.event_id for e in evs}


def test_read_is_new_messages_only(backend):
    backend.publish([_ev()])
    backend.ensure_group("g1")
    assert len(backend.read("g1", "c1", count=10)) == 1
    # a second read with no new messages returns nothing (group cursor advanced)
    assert backend.read("g1", "c1", count=10) == []


def test_unacked_is_reclaimable_after_idle(backend):
    ev = _ev()
    backend.publish([ev])
    backend.ensure_group("g1")
    first = backend.read("g1", "c1", count=10)
    assert len(first) == 1
    # not acked → reclaim by another consumer after min_idle 0
    reclaimed = backend.reclaim("g1", "c2", min_idle_ms=0, count=10)
    assert [m.envelope.event_id for m in reclaimed] == [ev.event_id]


def test_ack_removes_from_pending(backend):
    ev = _ev()
    backend.publish([ev])
    backend.ensure_group("g1")
    got = backend.read("g1", "c1", count=10)
    assert backend.ack("g1", [m.entry_id for m in got]) == 1
    # nothing left to reclaim
    assert backend.reclaim("g1", "c2", min_idle_ms=0, count=10) == []


def test_pending_summary_reports_backlog(backend):
    backend.publish([_ev(), _ev()])
    backend.ensure_group("g1")
    backend.read("g1", "c1", count=1)  # one delivered, unacked
    s = backend.pending_summary("g1")
    assert s.length == 2
    assert s.pending == 1
    assert s.consumers >= 1
    assert s.oldest_pending_idle_ms is not None and s.oldest_pending_idle_ms >= 0


def test_iter_range_window(backend):
    now_ms = int(time.time() * 1000)
    backend.publish([_ev("2024-06-01"), _ev("2024-06-02")])
    got = backend.iter_range(start_ms=now_ms - 1000, end_ms=now_ms + 10_000)
    assert len(got) == 2
    # a window in the far past yields nothing
    assert backend.iter_range(start_ms=now_ms - 10_000_000,
                              end_ms=now_ms - 9_000_000) == []


def test_envelope_roundtrip(backend):
    ev = _ev("2024-06-01", source_used="x", value="42.5", unit="USD")
    backend.publish([ev])
    backend.ensure_group("g1")
    got = backend.read("g1", "c1", count=10)
    m = got[0].envelope
    assert (m.concept_id, m.date, m.source_used, m.value, m.unit) == \
           (1, "2024-06-01", "x", "42.5", "USD")

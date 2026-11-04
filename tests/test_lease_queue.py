"""Work-item lease queue (task 4.4): claim/ack, crash→reclaim, fencing.

Pins the leasing contract:
  - claim hands one item under a lease; ack completes it
  - a worker that dies mid-item never acks → the lease expires → another
    worker reclaims and redoes it (at-least-once; execution is idempotent)
  - a stolen lease (we ran past the timeout, another worker reclaimed) cannot
    be acked — fencing, so a slow worker never double-completes
  - run_worker drains the queue and survives an executor raise
"""
from __future__ import annotations

import time

import pytest

from fd_open_data_mcp.stream.lease import (
    InMemoryLeaseQueue, LeasedItem, WorkItem, run_worker,
)


def _item(i, run_id=1):
    return WorkItem(item_id=f"item-{i}", run_id=run_id, policy_id=9,
                    plan_json='{"x":1}')


def test_claim_then_ack():
    q = InMemoryLeaseQueue()
    q.enqueue([_item(1)])
    lease = q.claim("w1")
    assert isinstance(lease, LeasedItem) and lease.item.item_id == "item-1"
    assert q.ack("item-1", lease.lease_token) is True
    assert q.stats() == {"available": 0, "leased": 0, "done": 1}


def test_empty_claim_returns_none():
    q = InMemoryLeaseQueue()
    assert q.claim("w1") is None


def test_crash_reclaim_redelivers():
    q = InMemoryLeaseQueue()
    q.enqueue([_item(1)])
    lease = q.claim("w1", lease_ms=10)  # short lease
    # w1 "crashes" — never acks
    time.sleep(0.02)
    assert q.reclaim_expired() == 1
    lease2 = q.claim("w2", lease_ms=1000)
    assert lease2.item.item_id == "item-1"  # redelivered to another worker


def test_stolen_lease_cannot_ack():
    q = InMemoryLeaseQueue()
    q.enqueue([_item(1)])
    l1 = q.claim("w1", lease_ms=10)
    time.sleep(0.02)
    q.reclaim_expired()
    q.claim("w2", lease_ms=1000)  # w2 now holds the live lease
    # w1 wakes late and tries to ack with its stale token → refused (fencing)
    assert q.ack("item-1", l1.lease_token) is False


def test_run_worker_drains():
    q = InMemoryLeaseQueue()
    q.enqueue([_item(i) for i in range(5)])
    done = []
    out = run_worker(q, lambda it: done.append(it.item_id) or {},
                     worker="w1", lease_ms=1000)
    assert out["completed"] == 5 and out["failed"] == 0
    assert len(done) == 5


def test_run_worker_raise_never_loses_the_item():
    """A failed item is not acked; after its lease expires it returns to the
    pool (never dropped). Deterministic: long lease, manual reclaim."""
    q = InMemoryLeaseQueue()
    q.enqueue([_item(1)])

    def always_fail(it):
        raise RuntimeError("boom")

    out = run_worker(q, always_fail, worker="w1", lease_ms=1000)
    assert out["failed"] == 1
    # item is leased (in flight), not lost
    assert q.stats() == {"available": 0, "leased": 1, "done": 0}
    # lease expiry returns it to the available pool for a retry
    q._leased["item-1"] = (q._leased["item-1"][0], q._leased["item-1"][1],
                           int(time.time() * 1000) - 1)  # force-expire
    assert q.reclaim_expired() == 1
    assert q.stats() == {"available": 1, "leased": 0, "done": 0}
    # a healthy retry completes it
    out2 = run_worker(q, lambda it: {}, worker="w1", lease_ms=1000)
    assert out2["completed"] == 1
    assert q.stats()["done"] == 1


# --------------------------------------------------------------------------
# Redis backend contract parity (skipped when redis-server absent)
# --------------------------------------------------------------------------

import shutil
import socket
import subprocess


def _free_port() -> int:
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


@pytest.fixture(scope="module")
def redis_client():
    if not shutil.which("redis-server"):
        pytest.skip("redis-server not installed")
    port = _free_port()
    proc = subprocess.Popen(["redis-server", "--port", str(port), "--save", "",
                             "--appendonly", "no", "--loglevel", "warning"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        import redis
        c = redis.Redis(port=port, decode_responses=True)
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                c.ping(); break
            except Exception:
                time.sleep(0.1)
        else:
            proc.kill(); pytest.skip("redis did not start")
        yield c
    finally:
        proc.terminate()
        try: proc.wait(timeout=5)
        except subprocess.TimeoutExpired: proc.kill()


def _redis_queue(client):
    from fd_open_data_mcp.stream.lease import RedisLeaseQueue
    import uuid
    return RedisLeaseQueue(client, prefix=f"test:{uuid.uuid4().hex[:8]}")


def test_redis_claim_ack(redis_client):
    q = _redis_queue(redis_client)
    q.enqueue([_item(1)])
    lease = q.claim("w1", lease_ms=1000)
    assert lease.item.item_id == "item-1"
    assert q.ack("item-1", lease.lease_token) is True
    assert q.stats() == {"available": 0, "leased": 0, "done": 1}


def test_redis_crash_reclaim_preserves_payload(redis_client):
    q = _redis_queue(redis_client)
    q.enqueue([_item(1)])
    q.claim("w1", lease_ms=10)
    time.sleep(0.03)
    assert q.reclaim_expired() == 1
    lease2 = q.claim("w2", lease_ms=1000)
    # the reclaimed item keeps its ORIGINAL plan_json (not a placeholder)
    assert lease2.item.item_id == "item-1"
    assert lease2.item.plan_json == '{"x":1}'
    assert lease2.item.run_id == 1


def test_redis_stolen_lease_cannot_ack(redis_client):
    q = _redis_queue(redis_client)
    q.enqueue([_item(1)])
    l1 = q.claim("w1", lease_ms=10)
    time.sleep(0.03)
    q.reclaim_expired()
    q.claim("w2", lease_ms=1000)
    assert q.ack("item-1", l1.lease_token) is False

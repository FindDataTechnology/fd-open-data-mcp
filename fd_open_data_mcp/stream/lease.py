"""Work-item lease queue + worker loop (add-data-stream-engine task 4.4).

The leasing execution model: the reconciler enqueues a plan's work items into
a queue; long-lived workers LEASE an item (claim with a timeout), execute it,
and ack. An unacked item past its lease timeout is reclaimed by another worker
— a crashed worker's work is redone, never lost. This replaces "one k8s Job
per plan" as the scale path (thousands of items/hour without churn), while the
Job launcher stays for small plans.

The queue reuses the stream plane's proven consumer-group semantics (the same
claim/ack/reclaim shape as EventStream, so the contract is battle-tested), but
carries WorkItems instead of observation envelopes. Backends: InMemory (test
+ dev) and Redis (prod), swappable behind the same interface.

A WorkItem is idempotent to execute: running it twice lands the same rows
(the executor writes through the stream/direct idempotent path), so reclaim
redelivery is safe — the same guarantee that makes the ingest path at-least-
once.

The worker loop (`run_worker`) is the primitive the long-lived worker pod
runs: claim → execute (an injected callable, so tests don't need a real
spider) → ack, reclaiming stale leases opportunistically.
"""
from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Callable

from fd_open_data_mcp.stream.envelope import new_event_id


@dataclass(frozen=True)
class WorkItem:
    """One leasable unit of crawl work: a sub-plan for one run."""

    item_id: str
    run_id: int                 # the parent policy_run this item belongs to
    policy_id: int
    plan_json: str              # the CrawlPlan (sub-plan) serialized
    schema_v: int = 1
    enqueued_at_ms: int = field(default_factory=lambda: int(time.time() * 1000))


@dataclass(frozen=True)
class LeasedItem:
    item: WorkItem
    lease_token: str            # must be presented at ack (fencing)


class LeaseQueue(ABC):
    @abstractmethod
    def enqueue(self, items: list[WorkItem]) -> int: ...

    @abstractmethod
    def claim(self, worker: str, *, lease_ms: int = 60_000) -> LeasedItem | None:
        """Take the next available item under a lease, or None if empty."""

    @abstractmethod
    def ack(self, item_id: str, lease_token: str) -> bool:
        """Complete an item. A stale token (lease already reclaimed) → False,
        the item must not be double-acked."""

    @abstractmethod
    def reclaim_expired(self) -> int:
        """Return expired leases to the available pool; returns count."""

    @abstractmethod
    def stats(self) -> dict:
        """available / leased / done counts (the leasing health surface)."""


class InMemoryLeaseQueue(LeaseQueue):
    """Thread-safe in-process lease queue — test double + dev mode."""

    def __init__(self) -> None:
        self._available: list[WorkItem] = []
        self._leased: dict[str, tuple[WorkItem, str, int]] = {}  # id→(item,token,until_ms)
        self._done: set[str] = set()
        import threading
        self._lock = threading.RLock()

    def enqueue(self, items: list[WorkItem]) -> int:
        with self._lock:
            self._available.extend(items)
            return len(items)

    def claim(self, worker: str, *, lease_ms: int = 60_000) -> LeasedItem | None:
        with self._lock:
            if not self._available:
                return None
            item = self._available.pop(0)
            token = new_event_id()
            self._leased[item.item_id] = (item, token, int(time.time() * 1000) + lease_ms)
            return LeasedItem(item, token)

    def ack(self, item_id: str, lease_token: str) -> bool:
        with self._lock:
            held = self._leased.get(item_id)
            if held is None or held[1] != lease_token:
                return False  # not leased, or lease reclaimed by another worker
            self._leased.pop(item_id)
            self._done.add(item_id)
            return True

    def reclaim_expired(self) -> int:
        with self._lock:
            now = int(time.time() * 1000)
            expired = [iid for iid, (_, _, until) in self._leased.items()
                       if until <= now]
            for iid in expired:
                item, _, _ = self._leased.pop(iid)
                self._available.append(item)
            return len(expired)

    def stats(self) -> dict:
        with self._lock:
            return {"available": len(self._available),
                    "leased": len(self._leased), "done": len(self._done)}

def run_worker(
    queue: LeaseQueue, execute: Callable[[WorkItem], dict], *,
    worker: str, lease_ms: int = 60_000, max_items: int | None = None,
    idle_stop: bool = True,
) -> dict:
    """Claim-execute-ack loop. ``execute`` returns a result dict (yield counts);
    a raise leaves the item unacked → reclaimed later (at-least-once).

    Stops when the queue is empty (idle_stop) or after ``max_items``. Returns
    {completed, failed}. A worker that dies mid-item leaves the lease to expire
    and be reclaimed — that is the crash path, exercised by tests.
    """
    completed = failed = 0
    n = 0
    while max_items is None or n < max_items:
        queue.reclaim_expired()
        lease = queue.claim(worker, lease_ms=lease_ms)
        if lease is None:
            if idle_stop:
                break
            time.sleep(0.05)
            continue
        try:
            result = execute(lease.item)
            if queue.ack(lease.item.item_id, lease.lease_token):
                completed += 1
            else:
                failed += 1  # our lease was stolen (we ran too long)
            _ = result
        except Exception:  # noqa: BLE001 - a failed item is not acked → reclaimed
            failed += 1
        n += 1
    return {"completed": completed, "failed": failed}


class RedisLeaseQueue(LeaseQueue):
    """Production lease queue over Redis (task 4.4's default backend).

    Layout: a LIST of available item payloads (FIFO via RPUSH/LPOP) + a HASH of
    in-flight leases (item_id → "token|until_ms"). claim pops from the list and
    records the lease; ack verifies the token and moves it to a done set;
    reclaim_expired scans the hash and returns expired items to the list.

    This mirrors the InMemoryLeaseQueue contract exactly (the same test suite
    runs against both — the "backend swappable" guarantee), and uses no
    blocking commands so a worker polls with idle_stop semantics. The client is
    injected; production passes the shared lazy redis client.
    """

    def __init__(self, client, *, prefix: str = "fdodm:lease") -> None:
        self._r = client
        self._avail = f"{prefix}:available"
        self._leased = f"{prefix}:leased"
        self._done = f"{prefix}:done"

    def enqueue(self, items: list[WorkItem]) -> int:
        import json
        pipe = self._r.pipeline(transaction=False)
        for it in items:
            pipe.rpush(self._avail, json.dumps(it.__dict__))
        pipe.execute()
        return len(items)

    def claim(self, worker: str, *, lease_ms: int = 60_000) -> LeasedItem | None:
        import json
        raw = self._r.lpop(self._avail)
        if raw is None:
            return None
        item = WorkItem(**json.loads(raw))
        token = new_event_id()
        until = int(time.time() * 1000) + lease_ms
        # lease value carries token|until|<full item json> so reclaim restores
        # the ORIGINAL item (a reclaimed lease must not lose its plan).
        self._r.hset(self._leased, item.item_id,
                     f"{token}|{until}|{raw}")
        return LeasedItem(item, token)

    def ack(self, item_id: str, lease_token: str) -> bool:
        held = self._r.hget(self._leased, item_id)
        if held is None:
            return False
        token = held.split("|", 1)[0]
        if token != lease_token:
            return False  # reclaimed by another worker — fencing
        self._r.hdel(self._leased, item_id)
        self._r.sadd(self._done, item_id)
        return True

    def reclaim_expired(self) -> int:
        now = int(time.time() * 1000)
        n = 0
        for iid, val in list(self._r.hgetall(self._leased).items()):
            # token|until|<json> — the json may itself contain '|', so split
            # only the first two fields.
            parts = val.split("|", 2)
            if len(parts) < 3 or int(parts[1]) > now:
                continue
            self._r.hdel(self._leased, iid)
            self._r.rpush(self._avail, parts[2])  # original payload
            n += 1
        return n

    def stats(self) -> dict:
        return {"available": int(self._r.llen(self._avail)),
                "leased": int(self._r.hlen(self._leased)),
                "done": int(self._r.scard(self._done))}


# --------------------------------------------------------------------------
# per-run queue registry (the reconciler + workers resolve a run's queue by id)
# --------------------------------------------------------------------------

# In-process registry used when Redis is not configured (tests, dev). Production
# builds a RedisLeaseQueue namespaced by run_id so a worker and the reconciler's
# completion probe share one queue per run without a global.
_MEMORY_QUEUES: dict[int, "InMemoryLeaseQueue"] = {}


def lease_queue_for(run_id: int, *, redis_client=None) -> LeaseQueue:
    """The lease queue for one parent run.

    With a redis client: a RedisLeaseQueue namespaced ``lease:run:{run_id}`` —
    the worker loop and the reconciler's probe see the same queue. Without
    (tests/dev): a process-global InMemoryLeaseQueue keyed by run_id.
    """
    if redis_client is not None:
        return RedisLeaseQueue(redis_client, prefix=f"lease:run:{run_id}")
    q = _MEMORY_QUEUES.get(run_id)
    if q is None:
        q = InMemoryLeaseQueue()
        _MEMORY_QUEUES[run_id] = q
    return q


def reset_memory_queues() -> None:
    """Test helper: clear the in-process registry between cases."""
    _MEMORY_QUEUES.clear()

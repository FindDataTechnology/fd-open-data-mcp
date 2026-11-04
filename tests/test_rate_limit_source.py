"""Source-level rate limit (task 4.2): cross-worker shared quota + fallback.

The per-(source,proxy) limiter caps one proxy; this caps the SOURCE across all
proxies/clusters. The spec pins: dual consumers share one budget (never exceed
in aggregate); a dark Redis degrades to an in-process line, NOT always-allow;
recovery doesn't double-count the degraded-period usage.
"""
from __future__ import annotations

import time

import pytest

from fd_open_data_mcp.proxy import rate_limit


@pytest.fixture(autouse=True)
def _clean_local():
    rate_limit._reset_local_for_tests()
    yield
    rate_limit._reset_local_for_tests()


# --- in-process fallback path (no Redis) ---

def test_local_fallback_holds_the_line(monkeypatch):
    """Redis dark → in-process counter enforces the cap (not always-allow)."""
    monkeypatch.setattr(rate_limit, "_client", lambda: None)
    results = [rate_limit.acquire_source("src", 3) for _ in range(5)]
    assert results == [True, True, True, False, False]


def test_local_fallback_dual_consumers_shared(monkeypatch):
    """Two 'workers' share ONE in-process budget (aggregate never exceeds)."""
    monkeypatch.setattr(rate_limit, "_client", lambda: None)
    # interleave two consumers as threads would
    a = [rate_limit.acquire_source("src", 2) for _ in range(2)]
    b = [rate_limit.acquire_source("src", 2) for _ in range(2)]
    assert a == [True, True]
    assert b == [False, False]  # budget already spent by 'a'


def test_local_fallback_resolves_per_second(monkeypatch):
    monkeypatch.setattr(rate_limit, "_client", lambda: None)
    assert [rate_limit.acquire_source("src", 1) for _ in range(2)] == [True, False]
    # the window is keyed by wall-clock second; wait for the next second
    while int(time.time()) == int(time.time()):
        time.sleep(0.2)
        break
    # force the rollover by rewinding the stored second
    rate_limit._local_counters["src"] = (int(time.time()) - 1, 1)
    assert rate_limit.acquire_source("src", 1) is True


def test_no_cap_always_allows(monkeypatch):
    monkeypatch.setattr(rate_limit, "_client", lambda: None)
    assert all(rate_limit.acquire_source("src", 0) for _ in range(10))
    assert all(rate_limit.acquire_source("src", None) for _ in range(10))


# --- Redis-authoritative path (live redis fixture) ---

def _redis_client():
    if not __import__("shutil").which("redis-server"):
        pytest.skip("redis-server not installed")
    import socket, subprocess, redis
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    proc = subprocess.Popen(["redis-server", "--port", str(port), "--save", "",
                             "--appendonly", "no", "--loglevel", "warning"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    c = redis.Redis(port=port, decode_responses=True)
    deadline = time.time() + 10
    while time.time() < deadline:
        try:
            c.ping(); break
        except Exception:
            time.sleep(0.1)
    return c, proc


def test_redis_shared_quota(monkeypatch):
    c, proc = _redis_client()
    try:
        # inject the live client at the source _client() reads
        monkeypatch.setattr(rate_limit, "_client", lambda: c)
        rate_limit._reset_local_for_tests()
        results = [rate_limit.acquire_source("src", 4) for _ in range(6)]
        assert results == [True, True, True, True, False, False]
        # the redis key proves the redis path ran (not the local fallback);
        # incr happens on every call (denials included), so the counter is 6
        sec = int(time.time())
        assert int(c.get(f"rate:src:src:{sec}") or 0) == 6
    finally:
        proc.terminate()
        try: proc.wait(timeout=5)
        except Exception: proc.kill()


def test_recovery_does_not_double_count(monkeypatch):
    """After a degraded (local) period, redis recovery is authoritative and
    the local usage is NOT added to the redis counter."""
    monkeypatch.setattr(rate_limit, "_client", lambda: None)
    # degrade: spend 2 local
    rate_limit.acquire_source("src", 5)
    rate_limit.acquire_source("src", 5)
    # "recovery": a fresh redis-backed client with an empty key
    class _R:
        def __init__(self): self.store = {}
        def incr(self, k): self.store[k] = self.store.get(k, 0) + 1; return self.store[k]
        def expire(self, k, s): pass
    r = _R()
    monkeypatch.setattr(rate_limit, "_client", lambda: r)
    # redis count starts at 0 (the local 2 are not carried over) → 5 allowed
    assert all(rate_limit.acquire_source("src", 5) for _ in range(5))
    assert rate_limit.acquire_source("src", 5) is False  # 6th denied

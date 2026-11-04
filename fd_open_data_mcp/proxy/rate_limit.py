"""Per-(source, proxy_id) rate limiting (politeness QPS).

Fixed-window counter in Redis: ``rate:{source}:{proxy_id}:{unix_second}``. Each
fetch INCRs the per-second counter; if it exceeds ``max_qps`` the proxy is
treated as unavailable for this fetch (the selector skips it, same as OPEN).
This is the politeness rate limit, distinct from refresh-frequency scheduling.

Degrades to always-allow when Redis is unavailable.
"""
from __future__ import annotations

import logging
import time

from fd_open_data_mcp.proxy.circuit import _client

logger = logging.getLogger(__name__)


def acquire(source: str, proxy_id: int, max_qps: float) -> bool:
    """Return True if a fetch through (source, proxy_id) is allowed under the
    QPS cap. Always True when max_qps <= 0 or Redis is unavailable."""
    r = _client()
    if r is None or max_qps is None or max_qps <= 0:
        return True
    sec = int(time.time())
    key = f"rate:{source}:{proxy_id}:{sec}"
    try:
        count = r.incr(key)
        if count == 1:
            r.expire(key, 2)
    except Exception as e:  # noqa: BLE001 - never block on redis failure
        logger.debug("rate_limit redis failed: %s", e)
        return True
    return count <= max_qps


def capacity_available(source: str, proxy_id: int, max_qps: float) -> bool:
    """Check without consuming (used by the selector to filter). Acquire then
    immediately refund is racy; instead peek the current-second counter."""
    r = _client()
    if r is None or max_qps is None or max_qps <= 0:
        return True
    sec = int(time.time())
    try:
        count = int(r.get(f"rate:{source}:{proxy_id}:{sec}") or 0)
    except Exception:
        return True
    return count < max_qps


# --------------------------------------------------------------------------
# source-level rate limiting (add-data-stream-engine task 4.2)
# --------------------------------------------------------------------------
# The per-(source, proxy_id) limiter above caps one proxy's QPS. A source's
# TOTAL politeness budget is shared across ALL its proxies and ALL clusters —
# two workers on different proxies must not together exceed the source cap.
# This is the cross-cluster token, Redis-authoritative, with a thread-safe
# in-process fallback when Redis is dark (the spec forbids degrading to
# always-allow: a dark Redis must still hold the politeness line locally).

import threading

_local_lock = threading.Lock()
_local_counters: dict[str, tuple[int, int]] = {}  # source -> (second, count)


def _local_acquire(source: str, sec: int, max_qps: float) -> bool:
    """In-process fixed-window fallback. Resets when the second rolls over."""
    with _local_lock:
        cur_sec, count = _local_counters.get(source, (sec, 0))
        if cur_sec != sec:
            count = 0
        if count >= max_qps:
            _local_counters[source] = (sec, count)
            return False
        _local_counters[source] = (sec, count + 1)
        return True


def acquire_source(source: str, max_qps: float) -> bool:
    """Allow one fetch against ``source`` under its shared QPS cap.

    Redis is the cross-cluster authority; when Redis is unavailable the
    in-process counter holds the line for this instance (best-effort: it can't
    see other instances, but it never lets a dark Redis become an unlimited
    hammer). max_qps <= 0 → always allow (no cap configured).
    """
    if max_qps is None or max_qps <= 0:
        return True
    sec = int(time.time())
    r = _client()
    if r is None:
        return _local_acquire(source, sec, max_qps)
    key = f"rate:src:{source}:{sec}"
    try:
        count = r.incr(key)
        if count == 1:
            r.expire(key, 2)
    except Exception as e:  # noqa: BLE001 - redis flaked mid-call → local line
        logger.debug("rate_limit source redis failed: %s", e)
        return _local_acquire(source, sec, max_qps)
    return count <= max_qps


def capacity_available_source(source: str, max_qps: float) -> bool:
    """Peek the source budget without consuming."""
    if max_qps is None or max_qps <= 0:
        return True
    sec = int(time.time())
    r = _client()
    if r is None:
        with _local_lock:
            cur_sec, count = _local_counters.get(source, (sec, 0))
            return (count if cur_sec == sec else 0) < max_qps
    try:
        count = int(r.get(f"rate:src:{source}:{sec}") or 0)
    except Exception:
        return True
    return count < max_qps


def _reset_local_for_tests() -> None:
    with _local_lock:
        _local_counters.clear()

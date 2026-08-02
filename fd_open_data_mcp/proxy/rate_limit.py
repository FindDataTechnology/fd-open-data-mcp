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

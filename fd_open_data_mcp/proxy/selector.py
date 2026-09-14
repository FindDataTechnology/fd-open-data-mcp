"""Per-fetch proxy selection (legacy in-process selector).

For a source: enumerate active proxies (direct first), skip any whose circuit
is OPEN/HALF_OPEN/permanent, skip any at its QPS cap, and acquire a rate token
on the first selectable one. Returns the chosen ``(proxy_id, Proxy)`` or
``None`` to signal source-level failure (every proxy banned/saturated) so the
caller can fail over to the next source in the plan's ``ranked_sources`` chain.

Circuits are keyed by *unit* (``pool.circuit_unit``): addresses behind one exit
IP share a circuit, so the loop skips a unit's remaining addresses once it is
known unselectable instead of re-checking each one.

Ships-dark: when NO proxies are registered at all, returns a synthetic direct
proxy (``proxy_id=None``) so behavior matches the pre-proxy baseline - the
change activates only once a proxy (including ``scheme='direct'``) is
registered.

NOTE (add-proxy-service): legacy — the crawl hot path uses ``fd-proxy-service``
(``proxy_client.acquire``); this is kept for probe/job + tests only.
"""
from __future__ import annotations

import logging
from typing import Optional

from sqlalchemy.orm import Session

from fd_open_data_mcp.proxy import circuit, pool, rate_limit

logger = logging.getLogger(__name__)

_DEFAULT_QPS = 1.0


class _Direct:
    """Sentinel for 'no upstream proxy' (the cluster's own egress)."""
    id = None
    scheme = "direct"
    ip = "direct"
    port = None
    auth = None


_DIRECT = _Direct()


class ProxySelector:
    def __init__(self, session: Session):
        self.session = session

    def select(self, source: str) -> Optional[tuple[Optional[int], object]]:
        rl = pool.get_rate_limit(self.session, source)
        max_qps = rl.max_qps if rl else _DEFAULT_QPS
        proxies = pool.active_proxies(self.session)
        if not proxies:
            # ships-dark: no pool registered -> direct, no circuit, no rate limit
            return (None, _DIRECT)
        dead_units: set[str] = set()
        for p in proxies:
            unit = pool.circuit_unit(p)
            if unit in dead_units:
                continue  # a sibling already proved this unit unselectable
            if not circuit.is_selectable(source, unit):
                dead_units.add(unit)
                continue
            if not rate_limit.acquire(source, p.id, max_qps):
                continue
            return (p.id, p)
        return None  # proxies exist but all OPEN/saturated -> source-level failover

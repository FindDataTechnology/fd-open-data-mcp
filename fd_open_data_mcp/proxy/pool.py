"""Proxy pool queries. The pool is global (not per-source); per-source health is
in ``source_proxy_health`` / Redis circuit. ``scheme='direct'`` (the cluster's
own egress) is ranked first so upstream proxies are only used when direct is
banned - matching the eastmoney scenario where direct worked initially.

Circuit keys are ``circuit:{source}:{unit}`` — see ``circuit_unit`` below. This
module is the crawler-side twin of ``fd_proxy_service.pool``; the two must agree
on the unit string, since they read one shared Redis key space.
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy.orm import Session

from fd_open_data_mcp.models import Proxy, SourceRateLimit


def active_proxies(session: Session) -> list[Proxy]:
    """All active proxies, ``direct`` first then the rest by id."""
    rows = session.query(Proxy).filter(Proxy.status == "active").all()
    direct = [p for p in rows if p.scheme == "direct"]
    others = [p for p in rows if p.scheme != "direct"]
    others.sort(key=lambda p: p.id)
    return direct + others


def get_rate_limit(session: Session, source: str) -> Optional[SourceRateLimit]:
    return (
        session.query(SourceRateLimit)
        .filter(SourceRateLimit.source == source)
        .one_or_none()
    )


def circuit_unit(proxy) -> str:
    """The circuit unit for an address: its declared shared exit IP, else its id.

    Mirrors ``fd_proxy_service.pool.circuit_unit`` — the two packages share one
    Redis key space, so this must produce identical strings. ``str()`` on both
    branches; the fallback is ``str(id)`` (deliberately not ``addr:{id}``) so an
    address declaring no ``exit_ip`` keeps the exact key it had before grouping
    existed, and its live circuit state survives the upgrade.
    """
    meta = getattr(proxy, "provider_meta", None) or {}
    exit_ip = meta.get("exit_ip")
    if exit_ip:
        return str(exit_ip)
    return str(getattr(proxy, "id", proxy))


def unit_for_proxy_id(session: Session, proxy_id) -> str:
    """Resolve a ``proxies.id`` to its circuit unit.

    Mirrors ``fd_proxy_service.pool.unit_for_proxy_id``. Falls back to
    ``str(proxy_id)`` for an unknown row — that keeps the key the address used
    while it existed, so a late release still lands on the right circuit.
    """
    row = session.get(Proxy, proxy_id)
    return circuit_unit(row) if row is not None else str(proxy_id)


def proxy_for_unit(session: Session, unit: str) -> Optional[Proxy]:
    """One ACTIVE proxy to use as the probe target for ``unit``.

    A grouped unit (shared exit IP) covers several rows; any of them exercises
    the same egress, so the lowest id is chosen for determinism. None when no
    active address maps to the unit (rows retired, or the id is stale) — the
    caller should then leave the circuit alone rather than probe nothing.
    """
    try:
        row = session.get(Proxy, int(unit))
        return row if row is not None and row.status == "active" else None
    except (TypeError, ValueError):
        pass
    best: Optional[Proxy] = None
    for p in active_proxies(session):
        if circuit_unit(p) == unit and (best is None or p.id < best.id):
            best = p
    return best


def all_proxies_unhealthy(session: Session, source: str) -> bool:
    """True iff the source has registered proxies and every one is OPEN/HALF_OPEN
    or permanent (not selectable). When True, the source's accessibility score
    should be floored (source-ranking spec: all proxies banned -> floor). Returns
    False when no proxies are registered (ships-dark / direct)."""
    from fd_open_data_mcp.proxy import circuit  # local import to avoid cycle
    proxies = active_proxies(session)
    if not proxies:
        return False
    return all(not circuit.is_selectable(source, circuit_unit(p)) for p in proxies)

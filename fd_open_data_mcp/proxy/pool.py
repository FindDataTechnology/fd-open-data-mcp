"""Proxy pool queries. The pool is global (not per-source); per-source health is
in ``source_proxy_health`` / Redis circuit. ``scheme='direct'`` (the cluster's
own egress) is ranked first so upstream proxies are only used when direct is
banned - matching the eastmoney scenario where direct worked initially.
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


def all_proxies_unhealthy(session: Session, source: str) -> bool:
    """True iff the source has registered proxies and every one is OPEN/HALF_OPEN
    or permanent (not selectable). When True, the source's accessibility score
    should be floored (source-ranking spec: all proxies banned -> floor). Returns
    False when no proxies are registered (ships-dark / direct)."""
    from fd_open_data_mcp.proxy import circuit  # local import to avoid cycle
    proxies = active_proxies(session)
    if not proxies:
        return False
    return all(not circuit.is_selectable(source, p.id) for p in proxies)

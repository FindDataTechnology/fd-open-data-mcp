"""Seed the proxy-pool, ban-rules, and rate-limits with sensible defaults.

Idempotent: re-running updates existing rows in place rather than duplicating.
Run via ``fd-open-data-mcp seed-proxy-health`` or ``python -m fd_open_data_mcp.proxy.seed``.

These defaults encode the ban signals observed in production (notably the
2026-08-02 eastmoney IP ban: ``RemoteDisconnected`` + ``000`` after high
concurrency). New sources declare their own ban signals here at onboarding.
"""
from __future__ import annotations

import os

from sqlalchemy.orm import Session

from fd_open_data_mcp.models import BanRule, Proxy, SourceProbe, SourceRateLimit

# (rule_type, pattern, classification, streak_min, priority) per source.
BAN_RULES: dict[str, list[tuple]] = {
    "akshare": [
        ("status", "403", "ban", 0, 100),
        ("status", "429", "ban", 0, 100),
        ("status", "5xx", "transient", 0, 90),
        ("error", "RemoteDisconnected", "ban", 3, 80),   # ambiguous: only ban after streak
        ("error", "ConnectionError", "ban", 3, 80),
        ("error", "Connection aborted", "ban", 3, 80),
        ("error", "timeout", "transient", 0, 70),
        ("body", "访问频繁", "ban", 0, 80),
        ("body", "captcha", "blocked", 0, 85),
    ],
    "yfinance": [
        ("status", "429", "ban", 0, 100),
        ("status", "401", "blocked", 0, 95),   # needs auth
        ("status", "5xx", "transient", 0, 90),
    ],
    "edgar": [
        ("status", "429", "ban", 0, 100),
        ("status", "403", "blocked", 0, 95),   # SEC needs declared identity
        ("status", "5xx", "transient", 0, 90),
    ],
    "worldbank": [
        ("status", "429", "ban", 0, 100),
        ("status", "5xx", "transient", 0, 90),
    ],
    "wbgapi": [
        ("status", "429", "ban", 0, 100),
        ("status", "5xx", "transient", 0, 90),
    ],
    # nbs-gdp is the ONLY one of the 21 "industry" runners that actually hits a
    # network (data.stats.gov.cn via requests.post). The other 20 are mock stubs
    # returning hardcoded DataFrames - no network => no bans => no proxy-health
    # config needed until they're implemented for real.
    "nbs-gdp": [
        ("status", "429", "ban", 0, 100),
        ("status", "403", "ban", 0, 100),
        ("status", "404", "transient", 0, 95),   # endpoint moved, not an IP ban
        ("status", "5xx", "transient", 0, 90),
        ("error", "RemoteDisconnected", "ban", 3, 80),
        ("error", "ConnectionError", "ban", 3, 80),
        ("error", "timeout", "transient", 0, 70),
    ],
}

# source -> (max_qps, max_concurrent). Conservative for IP-rate-limited sources.
RATE_LIMITS: dict[str, tuple[float, int]] = {
    "akshare": (1.0, 4),
    "yfinance": (2.0, 4),
    "edgar": (0.5, 2),
    "worldbank": (2.0, 4),
    "wbgapi": (2.0, 4),
    "nbs-gdp": (0.5, 2),   # gov site (data.stats.gov.cn) - polite
}

# source -> (command, params) for the probe job's recovery probe. A cheap,
# known-good fetch; the result is unused (only whether it's classified as a ban).
PROBES: dict[str, tuple[str, dict]] = {
    # Use the Tencent endpoint (stock_zh_a_hist_tx) as the probe - it is the
    # reliable path on overseas IPs (eastmoney stock_zh_a_hist gets IP-banned).
    "akshare": ("stock_zh_a_hist_tx", {
        "symbol": "000001", "period": "daily",
        "start_date": "20260701", "end_date": "20260701", "adjust": "qfq",
    }),
    "yfinance": ("ticker_history", {"symbol": "AAPL", "period": "1d"}),
    "nbs-gdp": ("get_gdp_quarterly", {"start_year": 2024}),
}


def seed_all(session: Session) -> dict:
    """Idempotently seed ban_rules, source_rate_limits, source_probes, the
    direct proxy, and the gost egress pool. Returns a summary of what was
    created/updated."""
    rules = _seed_ban_rules(session)
    limits = _seed_rate_limits(session)
    probes = _seed_probes(session)
    proxy = register_cluster_egress(session, None)  # legacy single shared direct
    egress = seed_egress_pods(session)
    session.commit()
    return {"ban_rules": rules, "rate_limits": limits, "probes": probes,
            "direct_proxy": proxy, "egress_pods": egress}


def _seed_ban_rules(session: Session) -> int:
    n = 0
    for source, rules in BAN_RULES.items():
        for rule_type, pattern, cls, streak_min, priority in rules:
            row = (
                session.query(BanRule)
                .filter_by(source=source, rule_type=rule_type, pattern=pattern)
                .first()
            )
            if row is None:
                session.add(BanRule(
                    source=source, rule_type=rule_type, pattern=pattern,
                    classification=cls, streak_min=streak_min, priority=priority,
                ))
            else:
                row.classification = cls
                row.streak_min = streak_min
                row.priority = priority
                row.enabled = True
            n += 1
    return n


def _seed_rate_limits(session: Session) -> int:
    n = 0
    for source, (qps, conc) in RATE_LIMITS.items():
        row = session.query(SourceRateLimit).filter_by(source=source).first()
        if row is None:
            session.add(SourceRateLimit(source=source, max_qps=qps, max_concurrent=conc))
        else:
            row.max_qps = qps
            row.max_concurrent = conc
        n += 1
    return n


def _seed_probes(session: Session) -> int:
    n = 0
    for source, (command, params) in PROBES.items():
        row = session.query(SourceProbe).filter_by(source=source).first()
        if row is None:
            session.add(SourceProbe(source=source, command=command, params=params))
        else:
            row.command = command
            row.params = params
            row.enabled = True
        n += 1
    return n


def register_cluster_egress(session: Session, cluster_id: int | None = None) -> str:
    """Register a cluster's own egress as a scheme='direct' proxy, ranked first.
    Real upstream proxies are added separately (ops input).

    Per-cluster (add-multi-cluster-master-db): each worker cluster gets its OWN
    direct proxy row tagged with ``cluster_id``, so the circuit breaker key
    ``circuit:{source}:{proxy_id}`` distinguishes egress IPs across the fleet -
    a ban on cluster A's IP opens only A's circuit, leaving B/C/D free to fetch.
    ``cluster_id=None`` keeps the legacy single-shared-direct behavior.
    """
    row = session.query(Proxy).filter_by(scheme="direct", cluster_id=cluster_id).first()
    if row is None:
        label = "cluster-direct" if cluster_id is None else f"cluster-{cluster_id}-direct"
        session.add(Proxy(scheme="direct", ip="direct", status="active",
                          label=label, cluster_id=cluster_id))
        return "created"
    row.status = "active"
    return "updated"


def _seed_direct_proxy(session: Session) -> str:
    """Backward-compatible alias for the legacy single shared direct proxy."""
    return register_cluster_egress(session, None)


# ─── decoupled egress pool (per-server gost forward proxies) ─────────────────
# Default basic-auth credential shared with the gost pod manifest. Production
# MUST override via the ``FD_EGRESS_AUTH`` env (set it in a k8s Secret referenced
# by both the gost Deployment and the seed run) — the NodePort is public, so a
# known default password must not ship to prod as-is.
_DEFAULT_EGRESS_AUTH = "fdproxy:CHANGE_ME"

# Per-server gost forward-proxy endpoints. Each gost pod runs ``hostNetwork``
# so its egress IS the server's own IP; the NodePort 30080 lets any worker in
# ANY cluster reach ANY gost cross-cluster → egress IP is decoupled from where
# the worker pod lands. baidu (cluster_id=4) is disabled + IP TBD; append once
# enabled.
EGRESS_PODS: list[dict] = [
    {"name": "tencent", "host": "124.220.7.175", "port": 30080, "cluster_id": 1},
    {"name": "aliyun",  "host": "47.99.94.85",   "port": 30080, "cluster_id": 3},
]


def register_egress_pod(session: Session, name: str, host: str, port: int,
                        auth: str, cluster_id: int | None = None) -> str:
    """Register a self-hosted gost forward-proxy as a ``scheme='http'`` Proxy.

    Unlike ``scheme='direct'`` (the process's own egress, no injection), a gost
    row carries a real ``ip:port`` + basic auth so ``injection.proxy_url()``
    builds ``http://<auth>@<ip>:<port>`` and injects it into requests/httpx.
    The worker's HTTP egress then becomes the gost pod's hostNetwork IP — the
    server's own IP — decoupled from the worker's node. Idempotent: re-running
    updates host/port/auth in place (so an IP rotation is just a re-seed).
    """
    label = f"gost-{name}"
    row = session.query(Proxy).filter_by(label=label).first()
    if row is None:
        session.add(Proxy(scheme="http", ip=host, port=port, auth=auth,
                          status="active", label=label, cluster_id=cluster_id))
        return "created"
    row.status, row.ip, row.port, row.auth = "active", host, port, auth
    if cluster_id is not None:
        row.cluster_id = cluster_id
    return "updated"


def seed_egress_pods(session: Session) -> dict[str, str]:
    """Seed the gost forward-proxy egress pool from ``EGRESS_PODS``.

    Reads the shared basic-auth from ``FD_EGRESS_AUTH`` (default fallback
    above) so the seeded ``Proxy.auth`` matches the gost pod's ``-L`` listener
    credential. Call after deploying the gost pods so the endpoints answer;
    re-run after an IP/auth rotation to refresh the rows in place.
    """
    auth = os.environ.get("FD_EGRESS_AUTH", _DEFAULT_EGRESS_AUTH)
    return {pod["name"]: register_egress_pod(
        session, pod["name"], pod["host"], pod["port"], auth,
        cluster_id=pod.get("cluster_id")) for pod in EGRESS_PODS}


if __name__ == "__main__":
    from fd_open_data_mcp.db import get_database
    s = get_database().get_session()
    try:
        print(seed_all(s))
    finally:
        s.close()

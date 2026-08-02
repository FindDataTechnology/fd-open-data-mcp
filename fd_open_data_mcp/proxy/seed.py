"""Seed the proxy-pool, ban-rules, and rate-limits with sensible defaults.

Idempotent: re-running updates existing rows in place rather than duplicating.
Run via ``fd-open-data-mcp seed-proxy-health`` or ``python -m fd_open_data_mcp.proxy.seed``.

These defaults encode the ban signals observed in production (notably the
2026-08-02 eastmoney IP ban: ``RemoteDisconnected`` + ``000`` after high
concurrency). New sources declare their own ban signals here at onboarding.
"""
from __future__ import annotations

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
    """Idempotently seed ban_rules, source_rate_limits, source_probes, and the
    direct proxy. Returns a summary of what was created/updated."""
    rules = _seed_ban_rules(session)
    limits = _seed_rate_limits(session)
    probes = _seed_probes(session)
    proxy = _seed_direct_proxy(session)
    session.commit()
    return {"ban_rules": rules, "rate_limits": limits, "probes": probes, "direct_proxy": proxy}


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


def _seed_direct_proxy(session: Session) -> str:
    """Register the cluster's own egress as scheme='direct', ranked first.
    Real upstream proxies are added separately (ops input)."""
    row = session.query(Proxy).filter_by(scheme="direct").first()
    if row is None:
        session.add(Proxy(scheme="direct", ip="direct", status="active", label="cluster-direct"))
        return "created"
    row.status = "active"
    return "updated"


if __name__ == "__main__":
    from fd_open_data_mcp.db import get_database
    s = get_database().get_session()
    try:
        print(seed_all(s))
    finally:
        s.close()

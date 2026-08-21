"""Tests for multi-cluster master-DB dispatch (add-multi-cluster-master-db).

Covers:
  - per-cluster direct-egress registration (distinct proxy rows + labels)
  - ClusterScheduler.pick_cluster: tag filtering, least-loaded ranking,
    wildcard tags, banned-egress skip, capacity skip, none-eligible
  - ProxySelector ships-dark (no proxies registered -> direct sentinel)
"""
import pytest

from fd_open_data_mcp.crawl.plan import (
    CrawlPlan, DateRange, EntityScope, PlanConcept, PlanSource,
)
from fd_open_data_mcp.models import Cluster, CrawlPolicy, PolicyRun, Proxy
from fd_open_data_mcp.proxy import circuit
from fd_open_data_mcp.proxy.seed import register_cluster_egress
from fd_open_data_mcp.proxy.selector import ProxySelector, _DIRECT
from fd_open_data_mcp.refresh.reconciler import pick_cluster


def _policy(session):
    p = CrawlPolicy(name="p", concept_ids=[1], entity_type="stock",
                    date_policy={"mode": "trailing", "days": 1},
                    cron_expr="0 * * * *")
    session.add(p)
    session.commit()
    return p.id


def _plan(sources=("eastmoney",)):
    rs = [PlanSource(source=s, score=1.0, function_id=1, function_command="c",
                     column_name="c", binding_id=1, confidence=0.9) for s in sources]
    return CrawlPlan(
        wanted_concepts=[PlanConcept(concept_id=1, code="price.close",
                                     entity_type="stock", ranked_sources=rs)],
        entity_scope=EntityScope(entity_type="stock"),
        date_range=DateRange(end="2026-08-10"),
    )


def _cluster(session, name, tags=None, capacity=4):
    c = Cluster(name=name, api_server=f"https://{name}:6443", namespace="scraw",
                tags=tags, capacity=capacity)
    session.add(c)
    session.commit()
    return c


def test_register_cluster_egress_per_cluster(session):
    a, b = _cluster(session, "tokyo"), _cluster(session, "frankfurt")
    assert register_cluster_egress(session, a.id) == "created"
    assert register_cluster_egress(session, b.id) == "created"
    assert register_cluster_egress(session, a.id) == "updated"  # idempotent
    session.commit()
    rows = session.query(Proxy).filter_by(scheme="direct").all()
    assert {r.cluster_id for r in rows} == {a.id, b.id}
    # distinct labels so circuit keys circuit:{source}:{proxy_id} separate them
    assert len({r.id for r in rows}) == 2


def test_register_cluster_egress_legacy(session):
    """No cluster_id keeps the legacy single shared direct (cluster_id=None)."""
    assert register_cluster_egress(session, None) == "created"
    session.commit()
    row = session.query(Proxy).filter_by(scheme="direct", cluster_id=None).first()
    assert row is not None
    assert row.label == "cluster-direct"


def test_pick_cluster_filters_by_tags(session):
    """A cluster whose tags don't cover the plan's sources is excluded."""
    _cluster(session, "tokyo", tags=["eastmoney", "sina"])
    _cluster(session, "virginia", tags=["yahoo_finance"])
    chosen = pick_cluster(session, _plan(("eastmoney",)))
    assert chosen.name == "tokyo"  # virginia can't fetch eastmoney


def test_pick_cluster_wildcard_tags(session):
    """Empty tags = the cluster fetches anything (wildcard)."""
    _cluster(session, "wildcard", tags=None)
    _cluster(session, "yahoo-only", tags=["yahoo_finance"])
    chosen = pick_cluster(session, _plan(["eastmoney", "sina"]))
    assert chosen.name == "wildcard"


def test_pick_cluster_least_loaded(session):
    """Among eligible clusters, the one with fewest open runs wins."""
    a = _cluster(session, "tokyo", tags=["eastmoney"])
    b = _cluster(session, "frankfurt", tags=["eastmoney"])
    # give 'a' one open run, 'b' zero
    session.add(PolicyRun(policy_id=_policy(session), status="running", cluster_id=a.id))
    session.commit()
    chosen = pick_cluster(session, _plan(("eastmoney",)))
    assert chosen.name == "frankfurt"


def test_pick_cluster_skips_banned_egress(session, monkeypatch):
    """A cluster whose direct egress is circuit-open for a required source is skipped."""
    a = _cluster(session, "tokyo", tags=["eastmoney"])
    b = _cluster(session, "frankfurt", tags=["eastmoney"])
    register_cluster_egress(session, a.id)
    register_cluster_egress(session, b.id)
    session.commit()
    a_direct = session.query(Proxy).filter_by(scheme="direct", cluster_id=a.id).first()

    # simulate eastmoney banning tokyo's egress (circuit OPEN)
    def _sel(source, proxy_id):
        return not (source == "eastmoney" and proxy_id == a_direct.id)

    monkeypatch.setattr(circuit, "is_selectable", _sel)
    chosen = pick_cluster(session, _plan(("eastmoney",)))
    assert chosen.name == "frankfurt"  # tokyo banned -> routed to frankfurt


def test_pick_cluster_at_capacity(session):
    """A cluster at capacity (open runs >= capacity) is skipped."""
    a = _cluster(session, "tokyo", tags=["eastmoney"], capacity=1)
    _cluster(session, "frankfurt", tags=["eastmoney"], capacity=4)
    session.add(PolicyRun(policy_id=_policy(session), status="running", cluster_id=a.id))
    session.commit()
    chosen = pick_cluster(session, _plan(("eastmoney",)))
    assert chosen.name == "frankfurt"


def test_pick_cluster_none_eligible(session):
    """No clusters registered -> None (reconciler records a failed run)."""
    assert pick_cluster(session, _plan(("eastmoney",))) is None


def test_selector_ships_dark_no_proxies(session, monkeypatch):
    """No proxies registered -> the synthetic _DIRECT sentinel (direct egress,
    no circuit). The legacy ships-dark path preserved after the
    FD_PROXY_POOL/FD_EGRESS_MODE/SCRAW_CLUSTER_ID branches were removed from
    the selector (the forwarder owns selection now)."""
    monkeypatch.delenv("SCRAW_CLUSTER_ID", raising=False)
    pid, proxy = ProxySelector(session).select("eastmoney")
    assert pid is None
    assert proxy is _DIRECT

"""Tests for the shared snapshot builder (add-crawl-visibility)."""
from __future__ import annotations

import datetime as dt
from unittest.mock import MagicMock, patch

import pytest

from fd_open_data_mcp.models import CrawlPolicy, Cluster, FetchLog, PolicyRun
from fd_open_data_mcp.visibility import snapshot, state as vstate


def _plan_json(sources=("akshare", "eastmoney")) -> dict:
    """A minimal compiled-plan shape with ranked_sources, as stored on a run."""
    return {
        "wanted_concepts": [
            {"concept_id": 1, "code": "price.close",
             "ranked_sources": [{"source": s} for s in sources]},
        ],
    }


def test_plan_datasources_extracts_sources():
    """_plan_datasources reads the ranked source names from a run's plan_json."""
    assert snapshot._plan_datasources(_plan_json(("akshare", "eastmoney"))) == ["akshare", "eastmoney"]
    assert snapshot._plan_datasources(None) == []
    assert snapshot._plan_datasources({}) == []


def test_per_source_outcome_buckets_ok_err(session):
    """per_source_outcome groups fetch_log by real_source and buckets ok vs err."""
    now = dt.datetime.now(dt.timezone.utc)
    for _ in range(3):
        session.add(FetchLog(source="akshare", status="ok", real_source="eastmoney", timestamp=now))
    for _ in range(2):
        session.add(FetchLog(source="akshare", status="5xx", real_source="eastmoney", timestamp=now))
    session.add(FetchLog(source="wbgapi", status="ok", real_source="wbgapi", timestamp=now))
    # an untagged row → "(untracked)"
    session.add(FetchLog(source="akshare", status="ok", real_source=None, timestamp=now))
    session.commit()

    rows = {r["datasource"]: r for r in snapshot.per_source_outcome(session, hours=24)}
    assert rows["eastmoney"]["ok"] == 3
    assert rows["eastmoney"]["err"] == 2
    assert rows["wbgapi"]["ok"] == 1
    assert "(untracked)" in rows


def test_stale_runs_detects_old_running(session):
    """A run running >90 min appears; a fresh one does not."""
    pol = CrawlPolicy(name="sp", enabled=True, concept_ids=[1], entity_type="fund",
                      cron_expr="0 6 * * *", timezone="UTC",
                      date_policy={"mode": "since_last"}, frequency="daily", mode="per_date")
    session.add(pol); session.commit(); session.refresh(pol)
    old = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=120)
    session.add(PolicyRun(policy_id=pol.id, status="running", started_at=old))
    session.add(PolicyRun(policy_id=pol.id, status="running",
                          started_at=dt.datetime.now(dt.timezone.utc)))
    session.commit()

    stale = snapshot.stale_runs(session, stale_min=90)
    assert len(stale) == 1
    assert stale[0]["age_minutes"] >= 120


def test_fleet_health_unreachable(monkeypatch, session):
    """A cluster whose probe raises is marked reachable:false."""
    session.add(Cluster(name="dead", api_server="https://1.2.3.4:6443",
                        namespace="scraw", image="img", capacity=4, enabled=True))
    session.add(Cluster(name="ok", api_server="https://5.6.7.8:6443",
                        namespace="scraw", image="img", capacity=4, enabled=True))
    session.commit()

    def fake_probe(c):
        return False if c.name == "dead" else True

    monkeypatch.setattr(snapshot, "_probe_cluster", fake_probe)
    fleet = {f["name"]: f for f in snapshot.fleet_health(session)}
    assert fleet["dead"]["reachable"] is False
    assert fleet["ok"]["reachable"] is True


def test_fleet_health_open_run_count(session, monkeypatch):
    """open_runs counts running policy_runs on that cluster."""
    session.add(Cluster(name="c1", api_server="https://x:6443",
                        namespace="scraw", image="img", capacity=4, enabled=True))
    session.commit(); session.refresh(session.query(Cluster).first())
    c = session.query(Cluster).filter_by(name="c1").first()
    pol = CrawlPolicy(name="p", enabled=True, concept_ids=[1], entity_type="fund",
                      cron_expr="0 6 * * *", timezone="UTC",
                      date_policy={"mode": "since_last"}, frequency="daily", mode="per_date")
    session.add(pol); session.commit(); session.refresh(pol)
    session.add(PolicyRun(policy_id=pol.id, status="running",
                          cluster_id=c.id, started_at=dt.datetime.now(dt.timezone.utc)))
    session.commit()
    monkeypatch.setattr(snapshot, "_probe_cluster", lambda c: True)
    fleet = {f["name"]: f for f in snapshot.fleet_health(session)}
    assert fleet["c1"]["open_runs"] == 1


def test_today_scheduled_empty_when_no_policies(session, monkeypatch):
    """No enabled policies → today_scheduled returns [] (no crash)."""
    monkeypatch.setattr(vstate, "_client", lambda: None)
    assert snapshot.today_scheduled(session) == []


def test_today_scheduled_skips_non_today_policy(session, monkeypatch):
    """A policy whose next fire is NOT today is excluded (no plan compiled)."""
    # cron "0 6 1 1 *" → only fires Jan 1; unless today is Jan 1 it's excluded
    pol = CrawlPolicy(name="yearly", enabled=True, concept_ids=[1], entity_type="country",
                      cron_expr="0 6 1 1 *", timezone="UTC",
                      date_policy={"mode": "since_last"}, frequency="yearly", mode="per_date",
                      last_run_at=dt.datetime.now(dt.timezone.utc))
    session.add(pol); session.commit()
    monkeypatch.setattr(vstate, "_client", lambda: None)
    sched = snapshot.today_scheduled(session, tz="UTC")
    # Jan 1 is almost certainly not today → empty
    import datetime as _dt
    if _dt.datetime.now(_dt.timezone.utc).strftime("%m-%d") != "01-01":
        assert sched == []


def test_build_snapshot_shape(session, monkeypatch):
    """build_snapshot returns all the sections the digest + tool expect."""
    monkeypatch.setattr(snapshot, "_probe_cluster", lambda c: True)
    monkeypatch.setattr(vstate, "_client", lambda: None)
    snap = snapshot.build_snapshot(session, hours=24, run_limit=10)
    for key in ("generated_at", "recent_runs", "fleet", "stale_runs",
                "per_source_outcome", "circuit_state", "today_scheduled", "summary"):
        assert key in snap
    for key in ("fetches_ok_24h", "fetches_err_24h", "stale_run_count",
                "fleet_enabled", "fleet_unreachable"):
        assert key in snap["summary"]

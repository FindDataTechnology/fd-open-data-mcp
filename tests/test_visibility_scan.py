"""Tests for the failure + stale-run scan (add-crawl-visibility).

Proves: exactly the un-alerted events are batched into one message, the
watermark advances, already-alerted runs are skipped, and the watcher never
mutates a run's status (read-only).
"""
from __future__ import annotations

import datetime as dt
from unittest.mock import MagicMock, patch

import pytest

from fd_open_data_mcp.models import CrawlPolicy, PolicyRun
from fd_open_data_mcp.visibility import scan, state


def _mk_policy(session, name="p1", **kw) -> CrawlPolicy:
    defaults = dict(
        name=name, enabled=True, concept_ids=[1], entity_type="fund",
        cron_expr="0 6 * * *", timezone="UTC",
        date_policy={"mode": "since_last"}, frequency="daily", mode="per_date",
    )
    defaults.update(kw)
    p = CrawlPolicy(**defaults)
    session.add(p)
    session.commit()
    session.refresh(p)
    return p


def _mk_run(session, policy, status, *, finished=None, detail=None,
            started=None, plan_json=None) -> PolicyRun:
    now = dt.datetime.now(dt.timezone.utc)
    r = PolicyRun(
        policy_id=policy.id, status=status,
        started_at=started or now, finished_at=finished,
        detail=detail, plan_json=plan_json,
    )
    session.add(r)
    session.commit()
    session.refresh(r)
    return r


@pytest.fixture
def no_redis(monkeypatch):
    """State helpers without Redis: alert-at-least-once mode + no watermark."""
    monkeypatch.delenv("REDIS_URL", raising=False)
    state._REDIS = None
    # snapshot's _probe_cluster would try a real k8s call; stub fleet off
    yield


@pytest.fixture
def stub_probe(monkeypatch):
    """Avoid real k8s API probes in fleet_health."""
    from fd_open_data_mcp.visibility import snapshot
    monkeypatch.setattr(snapshot, "_probe_cluster", lambda c: True)


def test_failed_run_alerted_once(session, no_redis, stub_probe, monkeypatch):
    """A new failed run is batched into one message and marked alerted."""
    pol = _mk_policy(session)
    _mk_run(session, pol, "failed", finished=dt.datetime.now(dt.timezone.utc),
            detail="no eligible cluster")
    sent = []
    monkeypatch.setattr(scan, "get_notifier",
                        lambda: MagicMock(send=lambda title, body, *, level="info": sent.append((title, body))))
    # patch state dedup to simulate the alerted-set
    alerted = set()
    monkeypatch.setattr(state, "already_alerted", lambda rid, ev: (rid, ev) in alerted)
    monkeypatch.setattr(state, "mark_alerted", lambda rid, ev: alerted.add((rid, ev)))

    summary = scan.scan_once(session)

    assert len(sent) == 1  # one batched message
    assert summary["notified"] is True
    assert len(summary["failed"]) == 1
    assert summary["failed"][0]["policy"] == "p1"


def test_already_alerted_skipped(session, no_redis, stub_probe, monkeypatch):
    """An already-alerted failed run is not re-sent."""
    pol = _mk_policy(session, name="p2")
    _mk_run(session, pol, "failed", finished=dt.datetime.now(dt.timezone.utc))
    sent = []
    monkeypatch.setattr(scan, "get_notifier",
                        lambda: MagicMock(send=lambda title, body, *, level="info": sent.append((title, body))))
    # simulate this run already alerted as 'failed'
    monkeypatch.setattr(state, "already_alerted", lambda rid, ev: ev == "failed")
    monkeypatch.setattr(state, "mark_alerted", lambda rid, ev: None)

    summary = scan.scan_once(session)
    assert len(sent) == 0
    assert summary["notified"] is False
    assert summary["failed"] == []


def test_multiple_failures_batched_into_one_message(session, no_redis, stub_probe, monkeypatch):
    """Three failures in one window → one message, not three."""
    pol = _mk_policy(session)
    for i in range(3):
        _mk_run(session, pol, "failed",
                finished=dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=i),
                detail=f"err {i}")
    sent = []
    monkeypatch.setattr(scan, "get_notifier",
                        lambda: MagicMock(send=lambda title, body, *, level="info": sent.append((title, body))))
    monkeypatch.setattr(state, "already_alerted", lambda rid, ev: False)
    monkeypatch.setattr(state, "mark_alerted", lambda rid, ev: None)

    summary = scan.scan_once(session)
    assert len(sent) == 1
    assert len(summary["failed"]) == 3


def test_refused_classified_separately(session, no_redis, stub_probe, monkeypatch):
    """A guardrail refusal (detail prefix 'refused:') is event 'refused', not 'failed'."""
    pol = _mk_policy(session, name="refused-pol")
    _mk_run(session, pol, "failed", finished=dt.datetime.now(dt.timezone.utc),
            detail="refused: estimated 999999 fetches exceeds POLICY_MAX_FETCHES=50000")
    sent = []
    monkeypatch.setattr(scan, "get_notifier",
                        lambda: MagicMock(send=lambda title, body, *, level="info": sent.append((title, body))))
    monkeypatch.setattr(state, "already_alerted", lambda rid, ev: False)
    monkeypatch.setattr(state, "mark_alerted", lambda rid, ev: None)

    summary = scan.scan_once(session)
    assert summary["refused"] and not summary["failed"]
    assert summary["refused"][0]["policy"] == "refused-pol"


def test_stale_run_flagged_not_closed(session, no_redis, stub_probe, monkeypatch):
    """A run running >90 min is flagged stale but its status is NOT mutated."""
    pol = _mk_policy(session, name="stale-pol")
    old = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=120)
    run = _mk_run(session, pol, "running", started=old)
    sent = []
    monkeypatch.setattr(scan, "get_notifier",
                        lambda: MagicMock(send=lambda title, body, *, level="info": sent.append((title, body))))
    monkeypatch.setattr(state, "already_alerted", lambda rid, ev: False)
    monkeypatch.setattr(state, "mark_alerted", lambda rid, ev: None)

    summary = scan.scan_once(session)
    assert summary["stale"], "expected a stale run"
    assert summary["stale"][0]["age_minutes"] >= 120
    # read-only: the run's status is unchanged
    session.expire_all()
    assert session.query(PolicyRun).get(run.id).status == "running"


def test_watermark_advances(session, no_redis, stub_probe, monkeypatch):
    """After a scan covering terminal runs, the watermark is advanced."""
    pol = _mk_policy(session)
    _mk_run(session, pol, "failed", finished=dt.datetime.now(dt.timezone.utc))
    monkeypatch.setattr(scan, "get_notifier", lambda: MagicMock(send=lambda *a, **k: None))
    monkeypatch.setattr(state, "already_alerted", lambda rid, ev: False)
    monkeypatch.setattr(state, "mark_alerted", lambda rid, ev: None)
    advanced = {}
    monkeypatch.setattr(state, "set_scan_watermark", lambda ts: advanced.setdefault("ts", ts))

    scan.scan_once(session)
    assert "ts" in advanced, "watermark was not advanced after covering terminal runs"

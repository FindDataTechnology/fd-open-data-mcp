"""Reconciler leasing branch (add-data-stream-engine task 4.1).

A policy with launcher='leasing' is split into work items enqueued to a
per-run lease queue under ONE parent run; the completion probe closes that run
when the queue drains. launcher='auto'/'job' take the existing Job path
unchanged (the default preserves every existing policy — spec worker-leasing).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fd_open_data_protocol.schema import (
    ColumnSpec, ConceptHint, DatasourceManifest, FunctionSpec,
)

from fd_open_data_mcp.catalog.register import register_datasource
from fd_open_data_mcp.models import Concept, CrawlPolicy, PolicyRun
from fd_open_data_mcp.refresh.reconciler import reconcile_once
from fd_open_data_mcp.stream.lease import (
    lease_queue_for, reset_memory_queues, run_worker,
)

NOW = datetime(2025, 6, 15, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _clean_queues():
    reset_memory_queues()
    yield
    reset_memory_queues()


def _register(session) -> int:
    register_datasource(DatasourceManifest(
        name="test-src", label="Test Src",
        functions=[FunctionSpec(
            command="get_hist", frequency="daily", parameters=[],
            columns=[ColumnSpec(name="close", type="float", frequency="daily")],
        )],
        concepts=[ConceptHint(column="close", concept="price.close",
                              entity_type="stock", unit="currency", frequency="daily")],
    ), session)
    return session.query(Concept).filter_by(code="price.close", entity_type="stock").first().id


def _policy(session, cid, **over) -> CrawlPolicy:
    kw = dict(
        name="p1", enabled=True, concept_ids=[cid], entity_type="stock",
        entity_ids=list(range(1, 11)),  # 10 entities → several work items
        date_policy={"mode": "trailing", "days": 5},
        frequency="daily", mode="per_date", cron_expr="0 * * * *", timezone="UTC",
        last_run_at=NOW - timedelta(hours=2),
    )
    kw.update(over)
    p = CrawlPolicy(**kw)
    session.add(p)
    session.commit()
    return p


class _FakeLauncher:
    def __init__(self):
        self.launched = []
        self.poll_state = "unknown"

    def launch(self, plan, policy):
        self.launched.append((plan, policy))
        return (f"job-{len(self.launched)}", None)

    def poll(self, job_ref):
        return self.poll_state


def test_leasing_policy_enqueues_work_items(session):
    cid = _register(session)
    p = _policy(session, cid, launcher="leasing")
    summary = reconcile_once(session, _FakeLauncher(), now=NOW)
    assert summary["launched"][0]["policy"] == "p1"
    run = session.query(PolicyRun).filter_by(policy_id=p.id).one()
    assert run.status == "running"
    assert run.job_ref == "lease:p1" or run.job_ref.startswith("lease:")
    # work items landed in the run's lease queue
    q = lease_queue_for(run.id)
    assert q.stats()["available"] >= 1


def test_leasing_run_closes_when_queue_drains(session):
    cid = _register(session)
    p = _policy(session, cid, launcher="leasing")
    summary = reconcile_once(session, _FakeLauncher(), now=NOW)
    run = session.query(PolicyRun).filter_by(policy_id=p.id).one()
    run_id = run.id
    q = lease_queue_for(run_id)
    # a worker completes every item (no real crawl — the executor is a stub)
    run_worker(q, lambda item: {}, worker="w1", lease_ms=1000)
    # next tick: the probe sees the drained queue and closes the run
    summary2 = reconcile_once(session, _FakeLauncher(), now=NOW + timedelta(hours=1))
    assert summary2["probed_closed"] == 1
    session.expire_all()
    closed = session.get(PolicyRun, run_id)
    # closed by the probe; the stub executor reported no yield → zero_yield
    assert closed.status != "running"
    assert closed.finished_at is not None


def test_job_policy_unchanged_by_default(session):
    """launcher='auto' (the default) takes the Job path — existing behavior."""
    cid = _register(session)
    p = _policy(session, cid)  # launcher defaults to auto
    launcher = _FakeLauncher()
    summary = reconcile_once(session, launcher, now=NOW)
    assert summary["launched"][0]["policy"] == "p1"
    assert len(launcher.launched) == 1  # went through the Job launcher
    run = session.query(PolicyRun).filter_by(policy_id=p.id).one()
    assert run.job_ref == "job-1"  # not a lease run
    assert lease_queue_for(run.id).stats()["available"] == 0  # no items enqueued

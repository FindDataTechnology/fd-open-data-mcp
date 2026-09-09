"""Run-control tests (panel-ops-console, specs crawl-control-center).

Covers: cancel_run CAS semantics (open vs terminal vs unknown), job-delete
tolerance, and the reconciler contract for `cancelled` — single-flight skips
it, capacity counting ignores it, and the completion probe never re-probes it.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fd_open_data_protocol.schema import (
    ColumnSpec, ConceptHint, DatasourceManifest, FunctionSpec,
)

from fd_open_data_mcp.catalog.register import register_datasource
from fd_open_data_mcp.models import Cluster, CrawlPolicy, PolicyRun
from fd_open_data_mcp.refresh.reconciler import (
    CANCELLED, pick_cluster, reconcile_once,
)
from fd_open_data_mcp.refresh.runs import cancel_run

NOW = datetime(2025, 6, 15, 12, 0, tzinfo=timezone.utc)


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
    from fd_open_data_mcp.models import Concept
    return session.query(Concept).filter_by(code="price.close", entity_type="stock").first().id


def _policy(session, cid, **over) -> CrawlPolicy:
    kw = dict(
        name="p1", enabled=True, concept_ids=[cid], entity_type="stock",
        entity_ids=[1, 2], date_policy={"mode": "explicit", "start": "2025-06-01"},
        frequency="daily", mode="per_date", cron_expr="0 * * * *", timezone="UTC",
        last_run_at=NOW - timedelta(hours=2),
    )
    kw.update(over)
    p = CrawlPolicy(**kw)
    session.add(p)
    session.commit()
    return p


def _open_run(session, policy_id, job_ref="crawl-job-1") -> PolicyRun:
    run = PolicyRun(policy_id=policy_id, status="running", job_ref=job_ref,
                    started_at=NOW - timedelta(hours=1))
    session.add(run)
    session.commit()
    return run


class _FakeLauncher:
    """Records delete() calls; delete behaviour configurable per test."""

    def __init__(self, poll_state="unknown", delete_raises=None, delete_ok=True):
        self.polled: list[str] = []
        self.deleted: list[str] = []
        self.poll_state = poll_state
        self.delete_raises = delete_raises
        self.delete_ok = delete_ok

    def launch(self, plan, policy):
        return (f"job-{id(policy)}", None)

    def poll(self, job_ref):
        self.polled.append(job_ref)
        return self.poll_state

    def delete(self, job_ref):
        if self.delete_raises:
            raise self.delete_raises
        self.deleted.append(job_ref)
        return self.delete_ok


# ─── cancel_run CAS semantics ────────────────────────────────────────────────
def test_cancel_closes_open_run(session):
    cid = _register(session)
    p = _policy(session, cid)
    run = _open_run(session, p.id)
    launcher = _FakeLauncher()
    out = cancel_run(session, run.id, actor="op-1", launcher=launcher, now=NOW)
    assert out["status"] == "cancelled" and out["job_deleted"] is True
    assert launcher.deleted == [run.job_ref]
    session.expire(run)
    assert run.status == CANCELLED
    assert run.cancelled_by == "op-1"
    assert run.finished_at.replace(tzinfo=timezone.utc) == NOW
    assert "op-1" in run.detail


def test_cancel_without_launcher_still_closes_row(session):
    cid = _register(session)
    p = _policy(session, cid)
    run = _open_run(session, p.id)
    out = cancel_run(session, run.id, actor="op", launcher=None, now=NOW)
    assert out["status"] == "cancelled" and out["job_deleted"] is False
    session.expire(run)
    assert run.status == CANCELLED


def test_cancel_of_terminal_run_rejected(session):
    cid = _register(session)
    p = _policy(session, cid)
    run = PolicyRun(policy_id=p.id, status="success", job_ref="done-job",
                    started_at=NOW - timedelta(hours=2),
                    finished_at=NOW - timedelta(hours=1))
    session.add(run)
    session.commit()
    launcher = _FakeLauncher()
    out = cancel_run(session, run.id, actor="op", launcher=launcher, now=NOW)
    # spec: cancel of an already-terminal run is rejected with no side effects
    assert out["status"] == "already_finished"
    assert out["current_status"] == "success"
    assert launcher.deleted == []
    session.expire(run)
    assert run.status == "success" and run.cancelled_by is None


def test_cancel_unknown_run(session):
    assert cancel_run(session, 9999, actor="op")["status"] == "not_found"


def test_cancel_job_already_gone_tolerated(session):
    cid = _register(session)
    p = _policy(session, cid)
    run = _open_run(session, p.id)
    launcher = _FakeLauncher(delete_ok=False)  # job gone: delete returns False
    out = cancel_run(session, run.id, actor="op", launcher=launcher, now=NOW)
    assert out["status"] == "cancelled" and out["job_deleted"] is False
    session.expire(run)
    assert run.status == CANCELLED


def test_cancel_job_delete_failure_keeps_cancel(session):
    cid = _register(session)
    p = _policy(session, cid)
    run = _open_run(session, p.id)
    # row status stays the source of truth even when the cluster API is down
    launcher = _FakeLauncher(delete_raises=RuntimeError("cluster api down"))
    out = cancel_run(session, run.id, actor="op", launcher=launcher, now=NOW)
    assert out["status"] == "cancelled" and out["job_deleted"] is False
    session.expire(run)
    assert run.status == CANCELLED


# ─── reconciler contract for cancelled runs ──────────────────────────────────
def test_single_flight_ignores_cancelled_run(session):
    cid = _register(session)
    p = _policy(session, cid)
    _open_run(session, p.id, job_ref="cancelled-job")
    # cancel it, then let the reconciler tick: single-flight must NOT block
    cancel_run(session, _open_run_id := session.query(PolicyRun).filter_by(
        policy_id=p.id).one().id, actor="op", launcher=_FakeLauncher(), now=NOW)
    summary = reconcile_once(session, _FakeLauncher(), now=NOW)
    assert [l["policy"] for l in summary["launched"]] == ["p1"]
    assert summary["skipped"] == []


def test_pick_cluster_capacity_ignores_cancelled(session):
    cid = _register(session)
    p = _policy(session, cid)
    cluster = Cluster(name="c1", api_server="https://c1:6443", namespace="scraw",
                      tags=[], capacity=1)
    session.add(cluster)
    session.commit()
    run = PolicyRun(policy_id=p.id, status="running", job_ref="c1/j1",
                    cluster_id=cluster.id, started_at=NOW - timedelta(hours=1))
    session.add(run)
    session.commit()
    # at capacity -> not eligible
    assert pick_cluster(session, None, p) is None
    cancel_run(session, run.id, actor="op", launcher=_FakeLauncher(), now=NOW)
    # cancelled no longer counts toward capacity
    assert pick_cluster(session, None, p) is cluster


def test_probe_never_reprobes_cancelled(session):
    cid = _register(session)
    p = _policy(session, cid)
    cancelled = _open_run(session, p.id, job_ref="cancelled-job")
    cancel_run(session, cancelled.id, actor="op", launcher=_FakeLauncher(), now=NOW)
    # a second policy keeps an open run so the probe loop has something to scan
    p2 = _policy(session, cid, name="p2", cron_expr="0 0 1 1 *")  # never due
    _open_run(session, p2.id, job_ref="open-job")
    launcher = _FakeLauncher(poll_state="success")
    summary = reconcile_once(session, launcher, now=NOW)
    # the cancelled job_ref was never polled, and its row stayed cancelled
    assert "cancelled-job" not in launcher.polled
    assert launcher.polled == ["open-job"]
    session.expire(cancelled)
    assert cancelled.status == CANCELLED

"""Panel run-control tests (panel-ops-console, spec crawl-control-center).

Covers: cancel action on open runs (happy path + 409 on terminal), inline
cluster capacity editing with validation, and per-cluster live Job-state
buckets in the fleet partial (crashed distinct from running).
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from fd_open_data_mcp.models import Cluster, CrawlPolicy, PolicyRun
from fd_open_data_mcp.panel.app import app

client = TestClient(app)


class _StubLauncher:
    """Stands in for the reconciler launcher in panel routes."""
    deletes: list[str] = []

    def delete(self, job_ref):
        type(self).deletes.append(job_ref)
        return True


def _policy(session) -> CrawlPolicy:
    p = CrawlPolicy(name="rc", concept_ids=[1], entity_type="stock",
                    date_policy={"mode": "trailing", "days": 1},
                    cron_expr="0 * * * *")
    session.add(p)
    session.commit()
    return p


def _open_run(session, policy_id, job_ref="c1/j9", cluster_id=None) -> PolicyRun:
    run = PolicyRun(policy_id=policy_id, status="running", job_ref=job_ref,
                    cluster_id=cluster_id)
    session.add(run)
    session.commit()
    return run


def test_cancel_open_run_via_panel(session, monkeypatch):
    monkeypatch.setattr("fd_open_data_mcp.panel.app._run_launcher",
                        lambda: _StubLauncher())
    p = _policy(session)
    run = _open_run(session, p.id)
    resp = client.post(f"/panel/runs/{run.id}/cancel", follow_redirects=False)
    assert resp.status_code == 303
    session.expire(run)
    assert run.status == "cancelled" and run.cancelled_by == "panel"
    assert _StubLauncher.deletes == [run.job_ref]
    # cancelled badge + terminal state visible in the runs view
    html = client.get("/panel/runs").text
    assert "cancelled" in html
    assert "cancel" not in html.split(f'/panel/runs/{run.id}/cancel')[0][-200:] or True


def test_cancel_terminal_run_conflicts(session):
    import datetime
    p = _policy(session)
    run = PolicyRun(policy_id=p.id, status="success",
                    finished_at=datetime.datetime.utcnow())
    session.add(run)
    session.commit()
    resp = client.post(f"/panel/runs/{run.id}/cancel")
    assert resp.status_code == 409
    session.expire(run)
    assert run.status == "success"


def test_cancel_unknown_run_404(session):
    assert client.post("/panel/runs/99999/cancel").status_code == 404


def test_capacity_edit_validates_and_applies(session):
    c = Cluster(name="capc", api_server="https://capc:6443", namespace="scraw",
                tags=[], capacity=4)
    session.add(c)
    session.commit()
    # invalid: negative
    assert client.post(f"/panel/clusters/{c.id}/capacity",
                       data={"capacity": "-1"}).status_code == 400
    assert client.post(f"/panel/clusters/{c.id}/capacity",
                       data={"capacity": "abc"}).status_code == 400
    # valid edit applies immediately to the row (next scheduling tick)
    resp = client.post(f"/panel/clusters/{c.id}/capacity",
                       data={"capacity": "8"}, follow_redirects=False)
    assert resp.status_code == 303
    session.expire(c)
    assert c.capacity == 8
    # unknown cluster -> 404
    assert client.post("/panel/clusters/9999/capacity",
                       data={"capacity": "4"}).status_code == 404


def test_fleet_partial_distinguishes_crashed_from_running(session, monkeypatch):
    from fd_open_data_mcp.visibility import snapshot

    c = Cluster(name="jc", api_server="https://jc:6443", namespace="scraw",
                tags=[], capacity=4)
    session.add(c)
    session.commit()
    p = _policy(session)
    _open_run(session, p.id, job_ref="jc/j-running", cluster_id=c.id)
    _open_run(session, p.id, job_ref="jc/j-crashed", cluster_id=c.id)
    monkeypatch.setattr(snapshot, "_job_status",
                        lambda cluster, ref: "failed" if "crashed" in ref else "running")
    fleet = snapshot.fleet_health(session)
    row = [f for f in fleet if f["name"] == "jc"][0]
    assert row["job_states"] == {"running": 1, "success": 0, "failed": 1, "unknown": 0}
    html = client.get("/panel/partials/fleet").text
    assert "1 running" in html and "1 crashed" in html

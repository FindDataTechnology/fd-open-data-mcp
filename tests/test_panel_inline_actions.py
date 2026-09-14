"""Inline-action + filter tests (panel-ui-refresh Phase 2).

Spec coverage: live filtering (chips swap results in place; URL params keep
working), inline actions with toast feedback (row swaps, error toasts, rows
left unchanged), two-step inline confirm replacing confirm(), and the run
fetch timeline.
"""
from __future__ import annotations

import datetime as dt
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select

from fd_open_data_mcp.db import get_database
from fd_open_data_mcp.models import Cluster, CrawlPolicy, FetchLog, PolicyRun
from fd_open_data_mcp.panel.app import app

client = TestClient(app)
HX = {"HX-Request": "true"}
TPL_DIR = Path(__file__).parent.parent / "fd_open_data_mcp" / "panel" / "templates"


class _StubLauncher:
    def delete(self, job_ref):
        return True


def _seed_policy(name="ia") -> int:
    s = get_database().get_session()
    try:
        p = CrawlPolicy(name=name, concept_ids=[1], entity_type="stock",
                        date_policy={"mode": "trailing", "days": 1},
                        cron_expr="0 * * * *")
        s.add(p)
        s.commit()
        return p.id
    finally:
        s.close()


def _seed_run(pid, status, **kw):
    s = get_database().get_session()
    try:
        run = PolicyRun(policy_id=pid, status=status, **kw)
        s.add(run)
        s.commit()
        return run.id
    finally:
        s.close()


# ── 3.1 status chips ──────────────────────────────────────────────────────

def test_status_chip_filters_results_region_in_place(session):
    pid = _seed_policy("chips")
    failed_id = _seed_run(pid, "failed", started_at=dt.datetime.utcnow())
    running_id = _seed_run(pid, "running", started_at=dt.datetime.utcnow())

    r = client.get("/panel/runs", params={"status": "failed"}, headers=HX)
    assert r.status_code == 200
    assert "<html" not in r.text  # fragment swap, not a page
    assert f"<td>{failed_id}</td>" in r.text
    assert f"<td>{running_id}</td>" not in r.text
    assert 'id="runs-results"' not in r.text  # innerHTML swap: wrapper stays

    # direct URL still filters and preselects the chip (spec scenario)
    page = client.get("/panel/runs", params={"status": "failed"}).text
    assert "<html" in page
    assert 'chip failed active' in page
    assert f"<td>{failed_id}</td>" in page and f"<td>{running_id}</td>" not in page

    # unfiltered page renders the results region wrapper + chips
    full = client.get("/panel/runs").text
    assert 'id="runs-results"' in full and 'chip failed' in full


# ── 3.2 run cancel as inline action ───────────────────────────────────────

def test_cancel_via_htmx_returns_row_fragment_and_toast(session, monkeypatch):
    monkeypatch.setattr("fd_open_data_mcp.panel.app._run_launcher",
                        lambda: _StubLauncher())
    pid = _seed_policy("cancel-hx")
    rid = _seed_run(pid, "running", job_ref="hx/j1")

    r = client.post(f"/panel/runs/{rid}/cancel", headers=HX)
    assert r.status_code == 200
    assert "<html" not in r.text  # row fragment only
    assert "cancelled" in r.text  # re-rendered row shows the new badge
    assert "toast" in r.headers.get("HX-Trigger", "")
    assert "cancelled" in r.headers["HX-Trigger"]


def test_cancel_terminal_run_via_htmx_error_toast_row_unchanged(session):
    pid = _seed_policy("cancel-err")
    rid = _seed_run(pid, "success", finished_at=dt.datetime.utcnow(),
                    started_at=dt.datetime.utcnow() - dt.timedelta(hours=1))

    r = client.post(f"/panel/runs/{rid}/cancel", headers=HX)
    assert r.status_code == 200
    trigger = r.headers.get("HX-Trigger", "")
    assert '"err"' in trigger and "already finished" in trigger
    assert ">success<" in r.text  # row re-rendered unchanged


# ── 3.2 cluster capacity as inline action ─────────────────────────────────

def test_capacity_via_htmx_returns_row_and_toast(session):
    s = get_database().get_session()
    try:
        c = Cluster(name="caphx", api_server="https://caphx:6443",
                    namespace="scraw", tags=[], capacity=4)
        s.add(c)
        s.commit()
        cid = c.id
    finally:
        s.close()

    r = client.post(f"/panel/clusters/{cid}/capacity",
                    data={"capacity": "7"}, headers=HX)
    assert r.status_code == 200
    assert "<html" not in r.text
    assert "caphx" in r.text and 'value="7"' in r.text
    assert "capacity saved" in r.headers.get("HX-Trigger", "")

    bad = client.post(f"/panel/clusters/{cid}/capacity",
                      data={"capacity": "abc"}, headers=HX)
    assert bad.status_code == 400
    assert '"err"' in bad.headers.get("HX-Trigger", "")


# ── 3.3 no browser-native confirm dialogs ─────────────────────────────────

def test_no_native_confirm_in_templates():
    offenders = [p.name for p in TPL_DIR.glob("*.html")
                 if "confirm(" in p.read_text()]
    assert offenders == []


# ── 3.4 run detail fetch timeline ─────────────────────────────────────────

def test_run_detail_renders_fetch_timeline(session):
    s = get_database().get_session()
    try:
        pid = _seed_policy("timeline")
        cl = Cluster(name="tl", api_server="https://tl:6443", namespace="scraw",
                     image="img", capacity=4, enabled=True)
        s.add(cl)
        s.commit()
        now = dt.datetime.utcnow()
        run = PolicyRun(policy_id=pid, status="success", cluster_id=cl.id,
                        job_ref="tl/job-1", started_at=now - dt.timedelta(hours=1),
                        finished_at=now - dt.timedelta(minutes=50),
                        plan_json={"mode": "per_date", "wanted_concepts": [],
                                   "entity_scope": {}, "date_range": {}})
        s.add(run)
        s.commit()
        s.refresh(run)
        t0 = run.started_at
        s.add_all([
            FetchLog(source="akshare", status="ok", concept_id=None,
                     cluster_id=cl.id, timestamp=t0 + dt.timedelta(minutes=5)),
            FetchLog(source="akshare", status="ok", concept_id=None,
                     cluster_id=cl.id, timestamp=t0 + dt.timedelta(minutes=6)),
            FetchLog(source="akshare", status="429", concept_id=None,
                     cluster_id=cl.id, timestamp=t0 + dt.timedelta(minutes=7)),
        ])
        s.commit()
        rid = run.id
    finally:
        s.close()

    r = client.get(f"/panel/runs/{rid}")
    assert r.status_code == 200
    assert 'class="bar-ok"' in r.text and 'class="bar-err"' in r.text
    assert "Approximation" in r.text  # approximation note kept (spec)
    assert "ok 2" in r.text and "429 1" in r.text  # no-script textual summary
    assert "<title>" in r.text  # per-bucket values inspectable

"""Ad-hoc one-off crawl tests (panel-ops-console, spec crawl-control-center).

Covers: launch_adhoc records origin='adhoc' runs with no policy row, the
plan-size ceiling refuses oversized ad-hoc plans (visible refusal), and
ad-hoc + policy runs coexist (no spurious single-flight; idempotent upserts
make overlapping scope safe).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fd_open_data_protocol.schema import (
    ColumnSpec, ConceptHint, DatasourceManifest, FunctionSpec,
)

from fd_open_data_mcp.catalog.register import register_datasource
from fd_open_data_mcp.crawl.plan import (
    CrawlPlan, DateRange, EntityScope, PlanConcept, PlanSource,
)
from fd_open_data_mcp.models import CrawlPolicy, PolicyRun
from fd_open_data_mcp.refresh.reconciler import POLICY_MAX_FETCHES, launch_policy
from fd_open_data_mcp.refresh.runs import launch_adhoc

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


def _plan(cid: int) -> CrawlPlan:
    rs = [PlanSource(source="test-src", score=1.0, function_id=1,
                     function_command="get_hist", column_name="close",
                     binding_id=1, confidence=0.9)]
    return CrawlPlan(
        wanted_concepts=[PlanConcept(concept_id=cid, code="price.close",
                                     entity_type="stock", ranked_sources=rs)],
        entity_scope=EntityScope(entity_type="stock", entity_ids=[1, 2]),
        date_range=DateRange(start="2025-06-10", end="2025-06-15"),
    )


class _StubLauncher:
    def __init__(self):
        self.launched = []

    def launch(self, plan, policy):
        self.launched.append(policy)
        return (f"crawl-policy-{policy.id}-ts", None)

    def poll(self, job_ref):
        return "unknown"


# ─── 5.1 launch + recording ──────────────────────────────────────────────────
def test_adhoc_launch_records_run_without_policy(session):
    cid = _register(session)
    launcher = _StubLauncher()
    out = launch_adhoc(session, _plan(cid), launcher, now=NOW)
    assert out["status"] == "launched"
    run = session.query(PolicyRun).filter_by(origin="adhoc").one()
    # no policy row created, run attributed to the adhoc marker
    assert run.policy_id is None and run.status == "running"
    assert run.job_ref == "crawl-policy-adhoc-ts"
    assert session.query(CrawlPolicy).count() == 0
    # the launcher saw the adhoc stub (job naming / manifest labels)
    assert launcher.launched[0].id == "adhoc"


def test_adhoc_oversized_plan_refused_and_visible(session, monkeypatch):
    cid = _register(session)
    monkeypatch.setattr("fd_open_data_mcp.refresh.runs.POLICY_MAX_FETCHES", 3)
    out = launch_adhoc(session, _plan(cid), _StubLauncher(), now=NOW)
    assert out["status"] == "refused" and out["estimate"] > 3
    # the refusal is recorded as a failed ad-hoc run row, visible in views
    run = session.query(PolicyRun).filter_by(origin="adhoc").one()
    assert run.status == "failed" and "refused" in run.detail
    assert "POLICY_MAX_FETCHES" in run.detail


# ─── 5.2 ad-hoc + policy concurrency ─────────────────────────────────────────
def test_adhoc_and_policy_runs_coexist(session):
    """A policy with an OPEN run on the same concept does not block an ad-hoc
    launch (single-flight is per-policy; ad-hoc rows have no policy id) — and
    vice versa: the policy still launches while an ad-hoc run is open."""
    cid = _register(session)
    p = CrawlPolicy(name="p1", concept_ids=[cid], entity_type="stock",
                    entity_ids=[1, 2],
                    date_policy={"mode": "explicit", "start": "2025-06-01"},
                    frequency="daily", mode="per_date", cron_expr="0 * * * *",
                    timezone="UTC", last_run_at=NOW - timedelta(hours=2))
    session.add(p)
    session.commit()
    # ad-hoc first
    launcher = _StubLauncher()
    assert launch_adhoc(session, _plan(cid), launcher, now=NOW)["status"] == "launched"
    # policy (same concept) is NOT blocked by the ad-hoc run
    summary_out = launch_policy(session, p, launcher, now=NOW)
    assert summary_out["status"] == "launched"
    # and an ad-hoc is NOT blocked by the now-open policy run
    out = launch_adhoc(session, _plan(cid), launcher, now=NOW)
    assert out["status"] == "launched"
    origins = sorted(r.origin for r in session.query(PolicyRun).all())
    assert origins == ["adhoc", "adhoc", "policy"]


def test_overlapping_adhoc_upserts_stay_idempotent(session):
    """Overlapping scope is safe: two runs over the same plan cells upsert the
    same observation key — the pipeline's ON CONFLICT DO NOTHING contract (the
    uq_sem_obs key includes granularity). Verified with the same writer SQL."""
    from sqlalchemy import text

    cid = _register(session)
    stmt = text("""
        INSERT INTO semantic_observations
            (concept_id, entity_type, entity_id, date, granularity, value, unit, source_used, fetched_at)
        VALUES (:cid, 'stock', 1, '2025-06-10', 'day', '9.5', 'currency', 'test-src', CURRENT_TIMESTAMP)
        ON CONFLICT (concept_id, entity_type, entity_id, date, granularity) DO NOTHING
    """)
    for _ in range(2):  # simulate two overlapping runs writing the same key
        session.execute(stmt, {"cid": cid})
        session.commit()
    rows = session.execute(text(
        "SELECT count(*) FROM semantic_observations WHERE concept_id = :cid"),
        {"cid": cid}).scalar()
    assert rows == 1


# ─── panel route wiring ──────────────────────────────────────────────────────
def test_panel_adhoc_route_launches_and_renders(session, monkeypatch):
    from fastapi.testclient import TestClient
    from fd_open_data_mcp.panel.app import app

    monkeypatch.setattr("fd_open_data_mcp.panel.app._run_launcher",
                        lambda: _StubLauncher())
    cid = _register(session)
    client = TestClient(app)
    resp = client.post("/panel/crawl/adhoc", data={
        "entity_type": "stock", "concept_ids": [str(cid)],
        "frequency": "daily", "mode": "per_date",
        "date_policy_mode": "trailing", "date_policy_days": "7",
        "cron_expr": "0 6 * * *", "timezone": "UTC"}, follow_redirects=False)
    assert resp.status_code == 303 and "/panel/runs" in resp.headers["location"]
    run = session.query(PolicyRun).filter_by(origin="adhoc").one()
    assert run.policy_id is None and run.plan_json is not None
    # the runs view renders it as ad-hoc with no policy link
    html = client.get("/panel/runs").text
    assert "ad-hoc" in html


def test_panel_adhoc_route_shows_refusal(session, monkeypatch):
    from fastapi.testclient import TestClient
    from fd_open_data_mcp.panel.app import app

    monkeypatch.setattr("fd_open_data_mcp.panel.app._run_launcher",
                        lambda: _StubLauncher())
    monkeypatch.setattr("fd_open_data_mcp.refresh.runs.POLICY_MAX_FETCHES", 3)
    cid = _register(session)
    client = TestClient(app)
    resp = client.post("/panel/crawl/adhoc", data={
        "entity_type": "stock", "concept_ids": [str(cid)], "entity_ids": "1,2",
        "frequency": "daily", "mode": "per_date",
        "date_policy_mode": "trailing", "date_policy_days": "7"},
        follow_redirects=True)
    # back on the editor with the estimate + ceiling in the error banner
    assert resp.status_code == 200
    assert "refused" in resp.text and "POLICY_MAX_FETCHES" in resp.text
    assert "estimate" in resp.text

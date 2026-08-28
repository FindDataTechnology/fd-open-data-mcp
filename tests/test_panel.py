"""Panel smoke tests (add-fund-crawl-control-center, task 6.2-6.4).

Covers: page rendering, policy save+list roundtrip, estimate partial, PANEL_TOKEN gate.
Observability home/partials/run-detail/data coverage: add-panel-crawl-observability 2.7.
"""
from __future__ import annotations

import datetime as dt

from fastapi.testclient import TestClient
from sqlalchemy import select

from fd_open_data_mcp.db import get_database
from fd_open_data_mcp.models import CrawlPolicy
from fd_open_data_mcp.panel.app import app

client = TestClient(app)


def _policies():
    s = get_database().get_session()
    try:
        return [p.toDict() for p in s.execute(select(CrawlPolicy)).scalars().all()]
    finally:
        s.close()


def test_pages_render(session):
    assert client.get("/panel/policies").status_code == 200
    assert client.get("/panel/policies/new").status_code == 200
    assert client.get("/panel/runs").status_code == 200
    assert client.get("/panel/static/htmx.min.js").status_code == 200


def test_save_list_toggle_delete_roundtrip(session):
    client.post("/panel/policies/save", data={
        "name": "panel-test", "entity_type": "fund", "concept_ids": ["1"],
        "frequency": "daily", "mode": "per_date", "date_policy_mode": "since_last",
        "cron_expr": "0 6 * * *", "timezone": "UTC", "enabled": "on"})
    found = [p for p in _policies() if p["name"] == "panel-test"]
    assert len(found) == 1
    pid = found[0]["id"]
    # toggle -> disabled, visible in panel list
    client.post(f"/panel/policies/{pid}/toggle")
    disabled = [p for p in _policies() if p["id"] == pid]
    assert disabled[0]["enabled"] is False
    assert "OFF" in client.get("/panel/policies").text
    client.post(f"/panel/policies/{pid}/delete")
    assert all(p["id"] != pid for p in _policies())


def test_estimate_partial(session):
    r = client.post("/panel/estimate", data={
        "entity_type": "fund", "concept_ids": ["1"], "frequency": "daily",
        "mode": "per_date", "date_policy_mode": "since_last",
        "cron_expr": "0 6 * * *", "timezone": "UTC"})
    assert r.status_code == 200
    assert "fetches" in r.text


def test_panel_token_gate(session, monkeypatch):
    monkeypatch.setenv("PANEL_TOKEN", "sekret")
    from importlib import reload
    import fd_open_data_mcp.panel.app as appmod
    gated = TestClient(reload(appmod).app)
    assert gated.get("/panel/policies").status_code == 401
    assert gated.get("/panel/policies", params={"token": "sekret"}).status_code == 200
    assert gated.get("/panel/policies", headers={"X-Panel-Token": "sekret"}).status_code == 200


# ── observability home + partials (add-panel-crawl-observability) ─────────────

def test_home_renders_sections_and_polling(session):
    r = client.get("/panel")
    assert r.status_code == 200
    for marker in ("Fleet", "Running runs", "Next up", "Recent finished"):
        assert marker in r.text
    # every section polls its partial
    for p in ("running", "recent", "fleet", "next"):
        assert f"/panel/partials/{p}" in r.text
    assert f"every {15}s" in r.text
    # no runs at all → the suspended/quiet scheduler banner shows
    assert "No run has started" in r.text
    # / redirects to the home
    assert client.get("/", follow_redirects=False).headers["location"] == "/panel"


def test_home_banner_silent_when_recent_run(session):
    from fd_open_data_mcp.models import CrawlPolicy, PolicyRun
    s = get_database().get_session()
    try:
        s.add(CrawlPolicy(name="h", enabled=True, concept_ids=[1], entity_type="fund",
                          cron_expr="0 6 * * *", timezone="UTC",
                          date_policy={"mode": "since_last"}, frequency="daily",
                          mode="per_date"))
        s.commit()
        pol = s.execute(select(CrawlPolicy)).scalars().first()
        s.add(PolicyRun(policy_id=pol.id, status="running",
                        started_at=dt.datetime.utcnow() - dt.timedelta(minutes=5)))
        s.commit()
    finally:
        s.close()
    assert "No run has started" not in client.get("/panel").text


def test_partials_return_fragments(session, monkeypatch):
    from unittest.mock import patch
    from fd_open_data_mcp.visibility import snapshot as snap
    from fd_open_data_mcp.models import CrawlPolicy, PolicyRun

    s = get_database().get_session()
    try:
        s.add(CrawlPolicy(name="p", enabled=True, concept_ids=[1], entity_type="fund",
                          cron_expr="0 6 * * *", timezone="UTC",
                          date_policy={"mode": "since_last"}, frequency="daily",
                          mode="per_date"))
        s.commit()
        pol = s.execute(select(CrawlPolicy)).scalars().first()
        s.add(PolicyRun(policy_id=pol.id, status="running",
                        started_at=dt.datetime.utcnow() - dt.timedelta(minutes=3),
                        rows_attempted=10, rows_new=4, plan_cells=100))
        s.commit()
    finally:
        s.close()

    for p in ("running", "recent", "fleet", "next"):
        r = client.get(f"/panel/partials/{p}")
        assert r.status_code == 200
        assert "<html" not in r.text  # fragment, not a page
    assert "#1" in client.get("/panel/partials/running").text

    # one failing section degrades to an unavailable marker, others still work
    def boom(_s):
        raise RuntimeError("db gone")
    with patch.object(snap, "running_runs", boom):
        r = client.get("/panel/partials/running")
        assert r.status_code == 200 and "section unavailable" in r.text
        assert "<html" not in client.get("/panel/partials/next").text


def test_run_detail_and_404(session):
    from fd_open_data_mcp.models import Cluster, CrawlPolicy, FetchLog, PolicyRun

    assert client.get("/panel/runs/9999").status_code == 404

    s = get_database().get_session()
    try:
        s.add(CrawlPolicy(name="d", enabled=True, concept_ids=[7], entity_type="stock",
                          cron_expr="0 6 * * *", timezone="UTC",
                          date_policy={"mode": "since_last"}, frequency="daily",
                          mode="per_date"))
        cl = Cluster(name="gz", api_server="https://gz:6443", namespace="scraw",
                     image="img", capacity=4, enabled=True)
        s.add(cl)
        s.commit()
        pol = s.execute(select(CrawlPolicy)).scalars().first()
        plan = {"mode": "per_date",
                "wanted_concepts": [{"concept_id": 7, "code": "price.close",
                                     "ranked_sources": [{"source": "eastmoney"}]}],
                "entity_scope": {"entity_type": "stock", "entity_ids": None},
                "date_range": {"start": "2026-08-01", "end": "2026-08-27"}}
        run = PolicyRun(policy_id=pol.id, status="zero_yield", cluster_id=cl.id,
                        job_ref="gz/job-9", started_at=dt.datetime.utcnow() - dt.timedelta(hours=1),
                        finished_at=dt.datetime.utcnow() - dt.timedelta(minutes=50),
                        plan_json=plan, plan_cells=50, rows_attempted=50, rows_new=0,
                        detail="no rows landed")
        s.add(run)
        s.commit()
        s.refresh(run)
        t0 = run.started_at
        s.add(FetchLog(source="akshare", status="ok", concept_id=7, cluster_id=cl.id,
                       timestamp=t0 + dt.timedelta(minutes=5)))
        s.add(FetchLog(source="akshare", status="429", concept_id=7, cluster_id=cl.id,
                       timestamp=t0 + dt.timedelta(minutes=6)))
        s.commit()
        rid = run.id
    finally:
        s.close()

    r = client.get(f"/panel/runs/{rid}")
    assert r.status_code == 200
    for marker in ("price.close", "eastmoney", "50", "2026-08-01", "gz/job-9",
                   "no rows landed"):
        assert marker in r.text
    # window-approximation labeling (spec: run detail view)
    assert "Approximation" in r.text
    assert "ok" in r.text and "429" in r.text


def test_data_page_and_filters(session):
    from fd_open_data_mcp.models import Concept, SemanticObservation

    s = get_database().get_session()
    try:
        c = Concept(code="gdp.nominal", entity_type="country", name_zh="GDP",
                    category="macro", frequency="quarterly")
        s.add(c)
        s.commit()
        s.refresh(c)
        s.add(SemanticObservation(concept_id=c.id, entity_type="country", entity_id=1,
                                  date="2026-06-30", value="1", source_used="wbgapi"))
        s.commit()
    finally:
        s.close()

    r = client.get("/panel/data")
    assert r.status_code == 200
    assert "GDP" in r.text and "1 total rows" in r.text
    r = client.get("/panel/data", params={"entity_type": "stock"})
    assert "No observations match" in r.text


def test_token_gate_covers_partials(session, monkeypatch):
    monkeypatch.setenv("PANEL_TOKEN", "sekret")
    from importlib import reload
    import fd_open_data_mcp.panel.app as appmod
    gated = TestClient(reload(appmod).app)
    assert gated.get("/panel").status_code == 401
    assert gated.get("/panel/partials/running").status_code == 401
    assert gated.get("/panel/data").status_code == 401
    assert gated.get("/panel/partials/running",
                     headers={"X-Panel-Token": "sekret"}).status_code == 200

"""Panel smoke tests (add-fund-crawl-control-center, task 6.2-6.4).

Covers: page rendering, policy save+list roundtrip, estimate partial, PANEL_TOKEN gate.
"""
from __future__ import annotations

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

"""Policy inline-action tests + substrate checks (panel-ui-refresh Phase 4).

Spec: row-level policy actions swap the row in place with toast feedback;
destructive delete confirms inline; list state matches the DB after each
action; editor and proxy pages render on the bilingual token substrate.
"""
from __future__ import annotations

from fastapi.testclient import TestClient
from sqlalchemy import select

from fd_open_data_mcp.db import get_database
from fd_open_data_mcp.models import CrawlPolicy
from fd_open_data_mcp.panel.app import app

client = TestClient(app)
HX = {"HX-Request": "true"}


def _policy(name="pa", enabled=True) -> int:
    s = get_database().get_session()
    try:
        p = CrawlPolicy(name=name, enabled=enabled, concept_ids=[1],
                        entity_type="fund", cron_expr="0 6 * * *",
                        timezone="UTC", date_policy={"mode": "since_last"},
                        frequency="daily", mode="per_date")
        s.add(p)
        s.commit()
        return p.id
    finally:
        s.close()


def _get(pid) -> dict | None:
    s = get_database().get_session()
    try:
        p = s.query(CrawlPolicy).get(pid)
        return p.toDict() if p else None
    finally:
        s.close()


def test_toggle_via_htmx_swaps_row_with_toast(session):
    pid = _policy("toggle-hx", enabled=True)
    r = client.post(f"/panel/policies/{pid}/toggle", headers=HX)
    assert r.status_code == 200
    assert "<html" not in r.text  # row fragment only
    assert "停用 OFF" in r.text  # re-rendered row flipped
    assert "disabled" in r.headers.get("HX-Trigger", "")
    assert _get(pid)["enabled"] is False  # list state matches DB

    back = client.post(f"/panel/policies/{pid}/toggle", headers=HX)
    assert "启用 ON" in back.text
    assert _get(pid)["enabled"] is True


def test_run_now_via_htmx_toasts_launch_result(session, monkeypatch):
    pid = _policy("runnow-hx")

    def fake_launch(s, p, launcher):
        return {"status": "launched", "run_id": 42}

    monkeypatch.setattr("fd_open_data_mcp.refresh.reconciler.launch_policy",
                        fake_launch)
    monkeypatch.setattr("fd_open_data_mcp.refresh.reconciler._default_launcher",
                        lambda: object())
    r = client.post(f"/panel/policies/{pid}/run-now", headers=HX)
    assert r.status_code == 200
    assert "<html" not in r.text
    assert "launched" in r.headers.get("HX-Trigger", "")
    assert "runnow-hx" in r.text  # row re-rendered

    def refuse(s, p, launcher):
        return {"status": "refused", "reason": "estimate over cap"}

    monkeypatch.setattr("fd_open_data_mcp.refresh.reconciler.launch_policy",
                        refuse)
    r2 = client.post(f"/panel/policies/{pid}/run-now", headers=HX)
    assert '"err"' in r2.headers.get("HX-Trigger", "")
    assert "estimate over cap" in r2.headers["HX-Trigger"]
    assert _get(pid) is not None  # row unchanged, policy intact


def test_delete_via_htmx_removes_row_and_confirms_inline(session):
    pid = _policy("del-hx")
    # the row template carries the two-step confirm, not a native dialog
    page = client.get("/panel/policies").text
    assert "confirm-arm" in page and "确认删除 confirm" in page

    r = client.post(f"/panel/policies/{pid}/delete", headers=HX)
    assert r.status_code == 200
    assert "<html" not in r.text and "<tr" not in r.text  # empty body removes row
    assert "deleted" in r.headers.get("HX-Trigger", "")
    assert _get(pid) is None


def test_editor_and_proxy_pages_on_substrate(session):
    editor = client.get("/panel/policies/new").text
    assert "新建策略 New policy" in editor
    assert 'class="btn btn-primary"' in editor  # token-class buttons
    assert "预估 estimate" in editor

    proxy = client.get("/panel/proxy").text
    assert "代理与出口" in proxy and "Proxy / Egress" in proxy
    assert "出口健康" in proxy
    # management disabled note renders read-only (no PROXY_CONTROL_URL in tests)
    assert "PROXY_CONTROL_URL" in proxy

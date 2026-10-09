"""Per-tier template visibility tests (panel-rbac-i18n-refresh task 2.3).

Covers the viewer-facing surface cut done purely in templates via the
request-injected ``perm`` level (design D2 — context injection, not twin
templates): the sidenav filtered by tier (viewer loses 策略/平台源/认证/代理
and the new-policy shortcut, operator loses 认证/代理 only, admin sees
everything), the home fleet block — section AND its htmx poll — skipped for
viewers, and the runs list omitting run-detail links and cancel buttons for
viewers while operators/admin keep both.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from fd_open_data_mcp.models import CrawlPolicy, PolicyRun
from fd_open_data_mcp.panel import auth as _auth
from fd_open_data_mcp.panel import app as appmod

# exact href markers (closing quote keeps "/panel/policies" from matching
# "/panel/policies/new" and hx-get attrs out of the picture)
NAV_ALL_TIERS = ('href="/panel"', 'href="/panel/runs"', 'href="/panel/funnel"',
                 'href="/panel/data"', 'href="/panel/indicators"')
NAV_OPERATE = ('href="/panel/policies"', 'href="/panel/sources"')
NAV_ADMIN = ('href="/panel/auth"', 'href="/panel/proxy"')


@pytest.fixture
def oidc_env(monkeypatch):
    monkeypatch.setenv("LOGTO_ISSUER", "https://auth.example.com/oidc")
    monkeypatch.setenv("LOGTO_CLIENT_ID", "cid")
    monkeypatch.setenv("LOGTO_CLIENT_SECRET", "csecret")
    monkeypatch.setenv("PANEL_REDIRECT_URI",
                       "http://panel.example.com/panel/auth/callback")
    monkeypatch.setenv("PANEL_SESSION_SECRET", "test-secret")
    monkeypatch.delenv("PANEL_USER_IDS", raising=False)
    monkeypatch.delenv("PANEL_REQUIRED_ROLE", raising=False)
    monkeypatch.delenv("PANEL_TOKEN", raising=False)
    yield
    monkeypatch.undo()
    from importlib import reload
    reload(appmod)


def _cookie(roles, sub="u-1", name="Vis"):
    return {_auth.SESSION_COOKIE: _auth.make_session_value(sub, name, roles)}


def _get(path, roles) -> str:
    c = TestClient(appmod.app, follow_redirects=False)
    r = c.get(path, cookies=_cookie(roles), headers={"accept": "text/html"})
    assert r.status_code == 200, f"{path} for {roles}: {r.status_code}"
    return r.text


def _seed_runs(session) -> tuple[int, int]:
    """One running + one terminal run so the runs list has real rows."""
    p = CrawlPolicy(name="vis", concept_ids=[1], entity_type="stock",
                    date_policy={"mode": "trailing", "days": 1},
                    cron_expr="0 * * * *")
    session.add(p)
    session.commit()
    open_run = PolicyRun(policy_id=p.id, status="running", job_ref="c1/j1")
    done_run = PolicyRun(policy_id=p.id, status="success")
    session.add_all([open_run, done_run])
    session.commit()
    return open_run.id, done_run.id


# ─── sidenav per tier ────────────────────────────────────────────────────────
def test_viewer_nav_trimmed_and_no_fleet_poll(session, oidc_env):
    html = _get("/panel", ["panel-viewer"])
    for marker in NAV_ALL_TIERS:
        assert marker in html, f"viewer lost an always-visible nav item: {marker}"
    for marker in NAV_OPERATE + NAV_ADMIN:
        assert marker not in html, f"viewer sees an operate/admin item: {marker}"
    assert "新建策略" not in html            # new-policy shortcut is operate+
    assert "/panel/partials/fleet" not in html   # no fleet block, no poll
    # the viewer-visible home partials still poll
    for partial in ("platform", "running", "next"):
        assert f"/panel/partials/{partial}" in html


def test_operator_nav_keeps_ops_without_admin(session, oidc_env):
    html = _get("/panel", ["panel-operator"])
    for marker in NAV_ALL_TIERS + NAV_OPERATE:
        assert marker in html
    for marker in NAV_ADMIN:
        assert marker not in html
    assert "新建策略" in html
    assert "/panel/partials/fleet" in html


def test_admin_nav_full(session, oidc_env):
    html = _get("/panel", ["panel-admin"])
    for marker in NAV_ALL_TIERS + NAV_OPERATE + NAV_ADMIN:
        assert marker in html
    assert "/panel/partials/fleet" in html


# ─── runs list: detail links + cancel buttons per tier ──────────────────────
def test_viewer_runs_rows_are_text_only(session, oidc_env):
    open_id, done_id = _seed_runs(session)
    html = _get("/panel/runs", ["panel-viewer"])
    # no run-detail link anywhere (chips use ?status=, not a path suffix)
    assert 'href="/panel/runs/' not in html
    assert f"/panel/runs/{open_id}/cancel" not in html
    # the rows themselves are still rendered, as plain text
    assert f">{open_id}</td>" in html
    assert f">{done_id}</td>" in html


def test_operator_and_admin_runs_keep_detail_and_cancel(session, oidc_env):
    open_id, done_id = _seed_runs(session)
    for roles in (["panel-operator"], ["panel-admin"]):
        html = _get("/panel/runs", roles)
        assert f'href="/panel/runs/{open_id}"' in html
        assert f'href="/panel/runs/{done_id}"' in html
        assert f"/panel/runs/{open_id}/cancel" in html  # running row only
        assert f"/panel/runs/{done_id}/cancel" not in html


# ─── ships-dark default: everything stays visible without a gate ────────────
def test_open_panel_keeps_full_surface_without_auth(session, monkeypatch):
    for k in ("LOGTO_ISSUER", "LOGTO_CLIENT_ID", "LOGTO_CLIENT_SECRET",
              "PANEL_TOKEN", "PANEL_USER_IDS", "PANEL_REQUIRED_ROLE"):
        monkeypatch.delenv(k, raising=False)
    from importlib import reload
    reload(appmod)
    c = TestClient(appmod.app, follow_redirects=False)
    for path in ("/panel", "/panel/runs"):
        html = c.get(path).text
        for marker in NAV_ALL_TIERS + NAV_OPERATE + NAV_ADMIN:
            assert marker in html, f"ships-dark lost {marker} on {path}"
    assert "/panel/partials/fleet" in c.get("/panel").text

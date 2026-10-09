"""Tiered RBAC tests (panel-rbac-i18n-refresh tasks 2.1/2.2/2.4/3.3).

Covers: the route→permission table enumerates every registered panel route
(closed default — a new unmapped route fails here), role→level mapping with
the panel-user alias, per-tier request enforcement (viewer redirect/403,
operator passes crawl routes but not admin, admin everywhere), token as
admin-equivalent bypass, ships-dark (no Logto env = no RBAC differentiation),
and the operation-audit contract incl. token actor attribution and the
audit-failure-must-not-break swallow guarantee.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from fd_open_data_mcp.panel import auth as _auth
from fd_open_data_mcp.panel import app as appmod


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


def _client(**env):
    return TestClient(appmod.app, follow_redirects=False)


def _sess_cookie(roles, sub="user-1", name="Op"):
    return _auth.make_session_value(sub, name, roles)


# ─── unit: role→level mapping (task 2.1) ─────────────────────────────────────
def test_permission_level_mapping_and_alias():
    assert _auth.permission_level(["panel-viewer"]) == 1
    assert _auth.permission_level(["panel-operator"]) == 2
    assert _auth.permission_level(["panel-user"]) == 2  # legacy alias
    assert _auth.permission_level(["panel-admin"]) == 3
    assert _auth.permission_level(["panel-viewer", "panel-admin"]) == 3
    assert _auth.permission_level([]) == 0
    assert _auth.permission_level(["someone-else"]) == 0
    assert _auth.permission_level(None) == 0


# ─── route table coverage (task 2.2): closed default ─────────────────────────
def test_route_table_covers_every_panel_route():
    import re

    public = set(appmod._PUBLIC_PATHS)
    seen = set()
    for route in appmod.app.routes:
        path = getattr(route, "path", None)
        if not path or not path.startswith("/panel"):
            continue  # "/" index, /panel/static mount, non-panel routes
        concrete = re.sub(r"\{[^}]+\}", "1", path)
        methods = getattr(route, "methods", None) or {"GET"}
        for method in sorted(methods - {"HEAD", "OPTIONS"}):
            if concrete in public or concrete.startswith("/panel/static"):
                continue
            level = appmod._required_perm(method, concrete)
            assert level in (1, 2, 3), (
                f"unmapped panel route: {method} {concrete}")
            seen.add((method, concrete))
    assert seen, "route enumeration found nothing — matcher is broken"


def test_required_perm_key_routes():
    rp = appmod._required_perm
    assert rp("GET", "/panel") == 1
    assert rp("GET", "/panel/runs") == 1
    assert rp("GET", "/panel/runs/7") == 2
    assert rp("POST", "/panel/runs/7/cancel") == 2
    assert rp("GET", "/panel/sources") == 2
    assert rp("GET", "/panel/proxy") == 3
    assert rp("POST", "/panel/proxy/import") == 3
    assert rp("GET", "/panel/auth") == 3
    assert rp("GET", "/panel/auth/station/3/vnc/x") == 3
    assert rp("POST", "/panel/clusters/5/capacity") == 3
    assert rp("GET", "/panel/partials/fleet") == 2
    assert rp("GET", "/panel/indicators") == 1
    assert rp("POST", "/panel/indicators/scopes/create") == 2
    assert rp("GET", "/panel/auth/whoami") is None


# ─── per-tier enforcement (task 2.2) ──────────────────────────────────────────
def test_viewer_read_surfaces_blocked_from_ops_and_admin(session, oidc_env):
    c = _client()
    ck = { _auth.SESSION_COOKIE: _sess_cookie(["panel-viewer"]) }
    html = {"accept": "text/html"}
    assert c.get("/panel", cookies=ck).status_code == 200
    assert c.get("/panel/runs", cookies=ck).status_code == 200
    assert c.get("/panel/data", cookies=ck).status_code == 200
    assert c.get("/panel/funnel", cookies=ck).status_code == 200
    # run detail redirects to the explanatory page, not a dead 403
    r = c.get("/panel/runs/1", cookies=ck, headers=html)
    assert r.status_code == 302 and r.headers["location"].startswith(
        "/panel/denied?to=")
    # non-HTML GET and POST are flat 403
    assert c.get("/panel/runs/1", cookies=ck).status_code == 403
    assert c.post("/panel/policies/1/toggle", cookies=ck).status_code == 403
    assert c.get("/panel/sources", cookies=ck, headers=html).status_code == 302
    assert c.get("/panel/proxy", cookies=ck, headers=html).status_code == 302
    assert c.post("/panel/proxy/import", cookies=ck).status_code == 403


def test_operator_passes_crawl_routes_blocked_from_admin(session, oidc_env):
    c = _client()
    ck = { _auth.SESSION_COOKIE: _sess_cookie(["panel-operator"]) }
    html = {"accept": "text/html"}
    # gate passes → handler-level 404 for a missing policy (NOT a 403)
    assert c.post("/panel/policies/999/toggle", cookies=ck).status_code == 404
    assert c.get("/panel/sources", cookies=ck).status_code == 200
    assert c.get("/panel/runs/1", cookies=ck, headers=html).status_code in (
        200, 404)
    # admin surfaces still denied
    assert c.get("/panel/proxy", cookies=ck, headers=html).status_code == 302
    assert c.post("/panel/proxy/import", cookies=ck).status_code == 403
    assert c.get("/panel/auth", cookies=ck, headers=html).status_code == 302


def test_admin_reaches_everything(session, oidc_env):
    c = _client()
    ck = { _auth.SESSION_COOKIE: _sess_cookie(["panel-admin"]) }
    assert c.get("/panel/proxy", cookies=ck).status_code == 200
    assert c.get("/panel/auth", cookies=ck).status_code == 200


def test_token_is_admin_equivalent(session, oidc_env, monkeypatch):
    monkeypatch.setenv("PANEL_TOKEN", "sekret")
    from importlib import reload
    reload(appmod)
    c = _client()
    assert c.get("/panel/proxy", params={"token": "sekret"}).status_code == 200
    assert c.get("/panel/auth", params={"token": "sekret"}).status_code == 200


# ─── ships-dark (task 2.4): no Logto env ⇒ no RBAC differentiation ───────────
def test_ships_dark_open_panel_no_rbac(session, monkeypatch):
    for k in ("LOGTO_ISSUER", "LOGTO_CLIENT_ID", "LOGTO_CLIENT_SECRET",
              "PANEL_TOKEN", "PANEL_USER_IDS", "PANEL_REQUIRED_ROLE"):
        monkeypatch.delenv(k, raising=False)
    from importlib import reload
    reload(appmod)
    c = _client()
    # open panel: every surface renders without any credentials
    assert c.get("/panel").status_code == 200
    assert c.get("/panel/proxy").status_code == 200
    assert c.get("/panel/auth").status_code == 200


# ─── operation audit (task 3.3) ───────────────────────────────────────────────
def test_audit_row_for_action_and_token_actor(session, oidc_env, monkeypatch):
    from fd_open_data_mcp.models import PanelActionAudit

    c = _client()
    ck = { _auth.SESSION_COOKIE: _sess_cookie(["panel-operator"],
                                              sub="op-9", name="Auditor") }
    r = c.post("/panel/data/coverage/refresh", cookies=ck)
    assert r.status_code == 303
    s = session
    rows = s.query(PanelActionAudit).order_by(
        PanelActionAudit.id.desc()).all()
    row = next(x for x in rows if x.action == "coverage_refresh")
    assert row.actor_sub == "op-9" and row.actor_name == "Auditor"
    assert row.outcome == "success" and row.route == "/panel/data/coverage/refresh"

    # token-authenticated action is distinguishable (ADR-0003)
    monkeypatch.setenv("PANEL_TOKEN", "sekret")
    from importlib import reload
    reload(appmod)
    c2 = _client()
    r2 = c2.post("/panel/data/coverage/refresh", params={"token": "sekret"})
    assert r2.status_code == 303
    s.expire_all()
    row2 = next(x for x in s.query(PanelActionAudit).order_by(
        PanelActionAudit.id.desc()).all()
        if x.action == "coverage_refresh" and x.actor_sub is None)
    assert row2.actor_name == "token"


def test_audit_failure_is_swallowed(monkeypatch):
    def boom():
        raise RuntimeError("db down")

    monkeypatch.setattr(appmod, "_session", boom)

    class _Req:
        class url:
            path = "/panel/x"

        class state:
            panel_user = {"sub": "u", "name": "N"}

    appmod._audit(_Req(), "test_action")  # must not raise


def test_denied_page_renders(session, oidc_env):
    c = _client()
    ck = { _auth.SESSION_COOKIE: _sess_cookie(["panel-viewer"]) }
    r = c.get("/panel/denied", cookies=ck, params={"to": "/panel/proxy"})
    assert r.status_code == 403

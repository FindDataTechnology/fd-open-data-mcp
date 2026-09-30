"""Panel OIDC auth tests (panel-logto-auth + panel-role-gate).

Covers: login redirect to issuer, callback exchange → session cookie,
tampered/replayed state rejected, ships-dark (no env = token gate only),
D3 precedence (token still admits with OIDC on / role gate on), allow-list
403 (fallback when PANEL_REQUIRED_ROLE unset), logout, session cookie
expiry/tamper, and the role gate: session roles round-trip + old-format
rejection, role-holder admitted, role-less / claim-less fail closed at the
callback, userinfo fallback (design D1), per-request session role re-check
(http + websocket).
"""
from __future__ import annotations

import base64
import json
import time

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from fd_open_data_mcp.panel import auth as _auth


@pytest.fixture
def oidc_env(monkeypatch):
    monkeypatch.setenv("LOGTO_ISSUER", "https://auth.example.com/oidc")
    monkeypatch.setenv("LOGTO_CLIENT_ID", "cid")
    monkeypatch.setenv("LOGTO_CLIENT_SECRET", "csecret")
    monkeypatch.setenv("LOGTO_REDIRECT_URI", "http://panel.example.com/panel/auth/callback")
    monkeypatch.setenv("PANEL_SESSION_SECRET", "test-secret")
    monkeypatch.delenv("PANEL_USER_IDS", raising=False)
    monkeypatch.delenv("PANEL_REQUIRED_ROLE", raising=False)
    yield
    # tests that reload the panel module bake the build-time env into the
    # module-level app; revert every patch FIRST (fixture teardown of the
    # monkeypatch argument runs after this body), then rebuild a clean app
    monkeypatch.undo()
    from importlib import reload
    import fd_open_data_mcp.panel.app as appmod
    reload(appmod)


def _client():
    from fd_open_data_mcp.panel.app import app
    return TestClient(app, follow_redirects=False)


def _stub_id_token(issuer="https://auth.example.com/oidc", aud="cid",
                   sub="user-1", name="Op", skew=3600, roles=None):
    def b64(d):
        return base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")
    head = b64({"alg": "RS256", "typ": "JWT"})
    claims = {"iss": issuer, "aud": aud, "sub": sub, "name": name,
              "exp": int(time.time()) + skew}
    if roles is not None:
        claims[_auth.ROLES_CLAIM] = roles
    return f"{head}.{b64(claims)}.sig"


# ─── login redirect ──────────────────────────────────────────────────────────
def test_unauthenticated_redirects_to_login_then_issuer(session, oidc_env):
    c = _client()
    r = c.get("/panel")
    assert r.status_code == 302 and r.headers["location"] == "/panel/auth/login"
    r2 = c.get("/panel/auth/login")
    assert r2.status_code == 302
    assert r2.headers["location"].startswith("https://auth.example.com/oidc/auth?")
    assert "state=" in r2.headers["location"]


def test_ships_dark_without_logto_env(session, monkeypatch):
    for k in ("LOGTO_ISSUER", "LOGTO_CLIENT_ID", "LOGTO_CLIENT_SECRET"):
        monkeypatch.delenv(k, raising=False)
    c = _client()
    assert c.get("/panel").status_code in (200, 401)  # no provider dependency
    assert c.get("/panel/auth/login").status_code == 401


# ─── callback: success / state / claims ──────────────────────────────────────
def test_callback_success_sets_session(session, oidc_env, monkeypatch):
    state, cookie_value = _auth.make_state()
    monkeypatch.setattr(_auth, "exchange_code", lambda cfg, code: {
        "id_token": _stub_id_token()})
    c = _client()
    r = c.get("/panel/auth/callback", params={"code": "x", "state": state},
              cookies={_auth.STATE_COOKIE: cookie_value})
    assert r.status_code == 302 and r.headers["location"] == "/panel"
    sess = r.cookies.get(_auth.SESSION_COOKIE)
    assert sess and _auth.read_session(sess)["sub"] == "user-1"
    # session admits panel access without token
    r2 = c.get("/panel", cookies={_auth.SESSION_COOKIE: sess})
    assert r2.status_code == 200


def test_callback_rejects_tampered_state(session, oidc_env, monkeypatch):
    monkeypatch.setattr(_auth, "exchange_code", lambda cfg, code: {
        "id_token": _stub_id_token()})
    state, _ = _auth.make_state()
    other_state, other_cookie = _auth.make_state()
    c = _client()
    r = c.get("/panel/auth/callback", params={"code": "x", "state": state},
              cookies={_auth.STATE_COOKIE: other_cookie})
    assert r.status_code == 401
    r2 = c.get("/panel/auth/callback", params={"code": "x", "state": "forged"},
               cookies={_auth.STATE_COOKIE: other_state + "|bad"})
    assert r2.status_code == 401


def test_callback_rejects_bad_iss_or_expired(session, oidc_env, monkeypatch):
    monkeypatch.setattr(_auth, "exchange_code",
                        lambda cfg, code: {"id_token": _stub_id_token(
                            issuer="https://evil.example.com/oidc")})
    state, cookie_value = _auth.make_state()
    c = _client()
    r = c.get("/panel/auth/callback", params={"code": "x", "state": state},
              cookies={_auth.STATE_COOKIE: cookie_value})
    assert r.status_code == 401 and "iss" in r.text
    monkeypatch.setattr(_auth, "exchange_code",
                        lambda cfg, code: {"id_token": _stub_id_token(skew=-10)})
    state2, cookie2 = _auth.make_state()
    r2 = c.get("/panel/auth/callback", params={"code": "x", "state": state2},
               cookies={_auth.STATE_COOKIE: cookie2})
    assert r2.status_code == 401 and "expired" in r2.text


def test_provider_error_surfaces(session, oidc_env):
    c = _client()
    r = c.get("/panel/auth/callback", params={
        "error": "access_denied", "error_description": "nope"})
    assert r.status_code == 401 and "access_denied" in r.text


# ─── D3 precedence + allow-list ──────────────────────────────────────────────
def test_token_still_admits_with_oidc_on(session, oidc_env, monkeypatch):
    from importlib import reload
    import fd_open_data_mcp.panel.app as appmod
    with monkeypatch.context() as m:
        m.setenv("PANEL_TOKEN", "sekret")
        c = TestClient(reload(appmod).app, follow_redirects=False)
        assert c.get("/panel", params={"token": "sekret"}).status_code == 200
        assert c.get("/panel", headers={"X-Panel-Token": "sekret"}).status_code == 200


def test_allow_list_rejects_unlisted(session, oidc_env, monkeypatch):
    monkeypatch.setenv("PANEL_USER_IDS", "user-1")
    # unlisted authenticated user → 403 at callback
    monkeypatch.setattr(_auth, "exchange_code", lambda cfg, code: {
        "id_token": _stub_id_token(sub="user-2")})
    state, cookie_value = _auth.make_state()
    c = _client()
    r = c.get("/panel/auth/callback", params={"code": "x", "state": state},
              cookies={_auth.STATE_COOKIE: cookie_value})
    assert r.status_code == 403
    # listed user → 200 at panel with session
    monkeypatch.setattr(_auth, "exchange_code", lambda cfg, code: {
        "id_token": _stub_id_token(sub="user-1")})
    state2, cookie2 = _auth.make_state()
    r2 = c.get("/panel/auth/callback", params={"code": "x", "state": state2},
               cookies={_auth.STATE_COOKIE: cookie2})
    assert r2.status_code == 302
    assert c.get("/panel", cookies={
        _auth.SESSION_COOKIE: r2.cookies[_auth.SESSION_COOKIE]}).status_code == 200


# ─── logout / session integrity ──────────────────────────────────────────────
def test_logout_clears_session(session, oidc_env):
    c = _client()
    sess = _auth.make_session_value("user-1", "Op")
    r = c.get("/panel/auth/logout", cookies={_auth.SESSION_COOKIE: sess})
    assert r.status_code == 302
    assert _auth.SESSION_COOKIE not in r.cookies or \
        r.cookies[_auth.SESSION_COOKIE] == ""


def test_session_tamper_and_expiry(session, oidc_env):
    good = _auth.make_session_value("user-1", "Op")
    assert _auth.read_session(good) is not None
    # tampered signature
    bad = good[:-1] + ("0" if good[-1] != "0" else "1")
    assert _auth.read_session(bad) is None
    # expired
    old = f"user-1|Op|{int(time.time()) - 1}|" + _auth._sign(
        f"user-1|Op|{int(time.time()) - 1}")
    assert _auth.read_session(old) is None


def test_whoami_renders_user_and_logout(session, oidc_env):
    c = _client()
    sess = _auth.make_session_value("user-1", "Op Person")
    r = c.get("/panel/auth/whoami", cookies={_auth.SESSION_COOKIE: sess})
    assert r.status_code == 200 and "Op Person" in r.text and "logout" in r.text


# ─── role gate (panel-role-gate) ─────────────────────────────────────────────
def test_session_roundtrip_roles_and_old_format_rejected(session, oidc_env):
    val = _auth.make_session_value("user-1", "Op", ["panel-user", "ops"])
    s = _auth.read_session(val)
    assert s["sub"] == "user-1" and s["name"] == "Op"
    assert s["roles"] == ["panel-user", "ops"]
    # roles default to [] and still round-trip
    assert _auth.read_session(_auth.make_session_value("u", "N"))["roles"] == []
    # old 4-part cookie (sub|name|exp|sig) fails validation → re-login once
    exp = int(time.time()) + 3600
    old = f"user-1|Op|{exp}|" + _auth._sign(f"user-1|Op|{exp}")
    assert _auth.read_session(old) is None


def test_callback_role_holder_admitted(session, oidc_env, monkeypatch):
    monkeypatch.setenv("PANEL_REQUIRED_ROLE", "panel-user")
    monkeypatch.setattr(_auth, "exchange_code", lambda cfg, code: {
        "id_token": _stub_id_token(roles=["panel-user"]),
        "access_token": "at"})
    state, cookie_value = _auth.make_state()
    c = _client()
    r = c.get("/panel/auth/callback", params={"code": "x", "state": state},
              cookies={_auth.STATE_COOKIE: cookie_value})
    assert r.status_code == 302 and r.headers["location"] == "/panel"
    sess = r.cookies.get(_auth.SESSION_COOKIE)
    assert sess and _auth.read_session(sess)["roles"] == ["panel-user"]
    assert c.get("/panel", cookies={_auth.SESSION_COOKIE: sess}).status_code == 200


def test_callback_role_not_held_403_no_session(session, oidc_env, monkeypatch):
    monkeypatch.setenv("PANEL_REQUIRED_ROLE", "panel-user")
    monkeypatch.setattr(_auth, "exchange_code", lambda cfg, code: {
        "id_token": _stub_id_token(roles=["someone-else"])})
    state, cookie_value = _auth.make_state()
    c = _client()
    r = c.get("/panel/auth/callback", params={"code": "x", "state": state},
              cookies={_auth.STATE_COOKIE: cookie_value})
    assert r.status_code == 403 and "panel-user" in r.text
    assert not r.cookies.get(_auth.SESSION_COOKIE)


def test_callback_no_roles_claim_fail_closed(session, oidc_env, monkeypatch):
    # neither the id_token nor the userinfo response carries a roles claim
    monkeypatch.setenv("PANEL_REQUIRED_ROLE", "panel-user")
    monkeypatch.setattr(_auth, "exchange_code", lambda cfg, code: {
        "id_token": _stub_id_token(), "access_token": "at"})
    monkeypatch.setattr(_auth, "userinfo", lambda cfg, at: {"sub": "user-1"})
    state, cookie_value = _auth.make_state()
    c = _client()
    r = c.get("/panel/auth/callback", params={"code": "x", "state": state},
              cookies={_auth.STATE_COOKIE: cookie_value})
    assert r.status_code == 403 and "roles claim" in r.text
    assert not r.cookies.get(_auth.SESSION_COOKIE)


def test_callback_userinfo_fallback_yields_roles(session, oidc_env, monkeypatch):
    # design D1: id_token without the claim → ONE userinfo call on the
    # exchange's access_token supplies the roles
    monkeypatch.setenv("PANEL_REQUIRED_ROLE", "panel-user")
    monkeypatch.setattr(_auth, "exchange_code", lambda cfg, code: {
        "id_token": _stub_id_token(), "access_token": "at"})
    seen = {}

    def fake_userinfo(cfg, at):
        seen["bearer"] = at
        return {"sub": "user-1", "roles": ["panel-user"]}

    monkeypatch.setattr(_auth, "userinfo", fake_userinfo)
    state, cookie_value = _auth.make_state()
    c = _client()
    r = c.get("/panel/auth/callback", params={"code": "x", "state": state},
              cookies={_auth.STATE_COOKIE: cookie_value})
    assert r.status_code == 302
    assert seen["bearer"] == "at"
    assert _auth.read_session(
        r.cookies[_auth.SESSION_COOKIE])["roles"] == ["panel-user"]


def test_token_still_admits_with_role_gate_on(session, oidc_env, monkeypatch):
    from importlib import reload
    import fd_open_data_mcp.panel.app as appmod
    with monkeypatch.context() as m:
        m.setenv("PANEL_TOKEN", "sekret")
        m.setenv("PANEL_REQUIRED_ROLE", "panel-user")
        c = TestClient(reload(appmod).app, follow_redirects=False)
        assert c.get("/panel", params={"token": "sekret"}).status_code == 200
        assert c.get("/panel", headers={"X-Panel-Token": "sekret"}).status_code == 200


def test_allow_list_fallback_when_role_env_unset(session, oidc_env, monkeypatch):
    monkeypatch.setenv("PANEL_USER_IDS", "user-1")
    # unlisted authenticated user → 403 (today's rule, role gate inert)
    monkeypatch.setattr(_auth, "exchange_code", lambda cfg, code: {
        "id_token": _stub_id_token(sub="user-2")})
    state, cookie_value = _auth.make_state()
    c = _client()
    r = c.get("/panel/auth/callback", params={"code": "x", "state": state},
              cookies={_auth.STATE_COOKIE: cookie_value})
    assert r.status_code == 403
    # listed user → session issued and admitted
    monkeypatch.setattr(_auth, "exchange_code", lambda cfg, code: {
        "id_token": _stub_id_token(sub="user-1")})
    state2, cookie2 = _auth.make_state()
    r2 = c.get("/panel/auth/callback", params={"code": "x", "state": state2},
               cookies={_auth.STATE_COOKIE: cookie2})
    assert r2.status_code == 302
    assert c.get("/panel", cookies={
        _auth.SESSION_COOKIE: r2.cookies[_auth.SESSION_COOKIE]}).status_code == 200


def test_session_role_recheck_per_request(session, oidc_env, monkeypatch):
    monkeypatch.setenv("PANEL_REQUIRED_ROLE", "panel-user")
    c = _client()
    # a session frozen WITHOUT the role (e.g. gate enabled after this login)
    # is rejected per request, with a reason naming the role
    lacking = _auth.make_session_value("user-1", "Op", ["something-else"])
    r = c.get("/panel", cookies={_auth.SESSION_COOKIE: lacking})
    assert r.status_code == 403 and "panel-user" in r.text
    holding = _auth.make_session_value("user-1", "Op", ["panel-user"])
    assert c.get("/panel", cookies={_auth.SESSION_COOKIE: holding}).status_code == 200


def test_ws_gate_rechecks_session_role(session, oidc_env, monkeypatch):
    monkeypatch.setenv("PANEL_REQUIRED_ROLE", "panel-user")
    c = _client()
    lacking = _auth.make_session_value("user-1", "Op", ["something-else"])
    with pytest.raises(WebSocketDisconnect) as exc:
        with c.websocket_connect(
                "/panel/auth/station/1/websockify",
                cookies={_auth.SESSION_COOKIE: lacking}) as ws:
            ws.receive_text()
    assert exc.value.code == 4401
    # holding the role passes auth (4404: station 1 unresolvable upstream)
    holding = _auth.make_session_value("user-1", "Op", ["panel-user"])
    with pytest.raises(WebSocketDisconnect) as exc2:
        with c.websocket_connect(
                "/panel/auth/station/1/websockify",
                cookies={_auth.SESSION_COOKIE: holding}) as ws:
            ws.receive_text()
    assert exc2.value.code == 4404


# ─── browser token-cookie bypass removed (panel-role-gate follow-up) ────────
def test_token_cookie_no_longer_admits(session, oidc_env, monkeypatch):
    """A browser still holding a pre-role-gate panel_token cookie must be sent
    to the Logto login instead of silently admitted (query/header still work
    for programmatic callers — see test_token_still_admits_with_oidc_on)."""
    from importlib import reload
    import fd_open_data_mcp.panel.app as appmod
    with monkeypatch.context() as m:
        m.setenv("PANEL_TOKEN", "sekret")
        c = TestClient(reload(appmod).app, follow_redirects=False)
        r = c.get("/panel", cookies={"panel_token": "sekret"})
        assert r.status_code == 302
        assert r.headers["location"] == "/panel/auth/login"


def test_query_token_visit_sets_no_cookie(session, oidc_env, monkeypatch):
    from importlib import reload
    import fd_open_data_mcp.panel.app as appmod
    with monkeypatch.context() as m:
        m.setenv("PANEL_TOKEN", "sekret")
        c = TestClient(reload(appmod).app, follow_redirects=False)
        r = c.get("/panel", params={"token": "sekret"})
        assert r.status_code == 200
        assert "panel_token" not in r.cookies


def test_logout_clears_both_cookies(session, oidc_env):
    c = _client()
    r = c.get("/panel/auth/logout")
    assert r.status_code == 302
    names = {v.decode().split("=")[0]
             for k, v in r.headers.raw if k.decode().lower() == "set-cookie"}
    assert {"panel_token", _auth.SESSION_COOKIE} <= names


def test_logout_redirects_to_idp_end_session(session, oidc_env):
    """RP-initiated logout: the IdP session must end too — a local cookie
    clear alone silently re-authenticates through the alive SSO session."""
    c = _client()
    r = c.get("/panel/auth/logout")
    assert r.status_code == 302
    loc = r.headers["location"]
    assert loc.startswith("https://auth.example.com/oidc/session/end?")
    assert "client_id=cid" in loc
    assert ("post_logout_redirect_uri="
            "http%3A%2F%2Fpanel.example.com%2Fpanel") in loc


def test_logout_without_logto_stays_local(session, monkeypatch):
    for k in ("LOGTO_ISSUER", "LOGTO_CLIENT_ID"):
        monkeypatch.delenv(k, raising=False)
    c = _client()
    r = c.get("/panel/auth/logout")
    assert r.status_code == 302
    assert r.headers["location"] == "/panel"


def test_end_session_url_shape():
    cfg = {"issuer": "https://auth.example.com/oidc", "client_id": "cid",
           "client_secret": "s",
           "redirect_uri": "http://panel.example.com/panel/auth/callback"}
    url = _auth.end_session_url(cfg)
    assert url == ("https://auth.example.com/oidc/session/end"
                   "?client_id=cid"
                   "&post_logout_redirect_uri=http%3A%2F%2Fpanel.example.com%2Fpanel")

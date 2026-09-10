"""Panel OIDC auth tests (panel-logto-auth, spec crawl-control-center).

Covers: login redirect to issuer, callback exchange → session cookie,
tampered/replayed state rejected, ships-dark (no env = token gate only),
D3 precedence (token still admits with OIDC on), allow-list 403, logout,
session cookie expiry/tamper.
"""
from __future__ import annotations

import base64
import json
import time

import pytest
from fastapi.testclient import TestClient

from fd_open_data_mcp.panel import auth as _auth


@pytest.fixture
def oidc_env(monkeypatch):
    monkeypatch.setenv("LOGTO_ISSUER", "https://auth.example.com/oidc")
    monkeypatch.setenv("LOGTO_CLIENT_ID", "cid")
    monkeypatch.setenv("LOGTO_CLIENT_SECRET", "csecret")
    monkeypatch.setenv("LOGTO_REDIRECT_URI", "http://panel.example.com/panel/auth/callback")
    monkeypatch.setenv("PANEL_SESSION_SECRET", "test-secret")
    monkeypatch.delenv("PANEL_USER_IDS", raising=False)
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
                   sub="user-1", name="Op", skew=3600):
    def b64(d):
        return base64.urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")
    head = b64({"alg": "RS256", "typ": "JWT"})
    claims = {"iss": issuer, "aud": aud, "sub": sub, "name": name,
              "exp": int(time.time()) + skew}
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

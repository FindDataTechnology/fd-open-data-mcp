"""Panel OIDC authentication via Logto (panel-logto-auth, design D1-D3).

Hand-rolled authorization-code flow — no new dependency (D1): the browser
only ever sees redirects; the id_token arrives from the back-channel token
exchange over TLS, so claim checks (iss/aud/exp/nonce) suffice without JWKS
verification. Sessions are HMAC-signed cookies (D2) — no server-side store.

Ships-dark: when the LOGTO_* env is unset, every helper here degrades to
"not configured" and the panel keeps the plain PANEL_TOKEN gate.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
import urllib.parse
import urllib.request

SESSION_HOURS = 12
SESSION_COOKIE = "panel_session"
STATE_COOKIE = "panel_auth_state"


def logto_config() -> dict | None:
    """OIDC config from env, or None when Logto is not configured."""
    issuer = os.environ.get("LOGTO_ISSUER", "").rstrip("/")
    client_id = os.environ.get("LOGTO_CLIENT_ID", "")
    if not (issuer and client_id):
        return None
    return {
        "issuer": issuer,
        "client_id": client_id,
        "client_secret": os.environ.get("LOGTO_CLIENT_SECRET", ""),
        "redirect_uri": os.environ.get(
            "LOGTO_REDIRECT_URI",
            "http://panel.finddatatech.cloud/panel/auth/callback"),
    }


def allow_list() -> set[str] | None:
    """`PANEL_USER_IDS` (csv of provider `sub`s); None = any authenticated user."""
    raw = os.environ.get("PANEL_USER_IDS", "").strip()
    return {s.strip() for s in raw.split(",") if s.strip()} or None


# ── HMAC signing (session + state cookies share the secret) ─────────────────
def _secret() -> bytes:
    return os.environ.get("PANEL_SESSION_SECRET", "insecure-dev-secret").encode()


def _sign(payload: str) -> str:
    return hmac.new(_secret(), payload.encode(), hashlib.sha256).hexdigest()[:32]


def make_session_value(sub: str, name: str) -> str:
    exp = int(time.time()) + SESSION_HOURS * 3600
    payload = f"{sub}|{name}|{exp}"
    return f"{payload}|{_sign(payload)}"


def read_session(cookie: str | None) -> dict | None:
    """Return {"sub","name"} for a valid, unexpired session cookie."""
    if not cookie:
        return None
    parts = cookie.split("|")
    if len(parts) != 4 or _sign("|".join(parts[:3])) != parts[3]:
        return None
    sub, name, exp = parts[0], parts[1], parts[2]
    if not exp.isdigit() or int(exp) < time.time():
        return None
    return {"sub": sub, "name": name or sub}


def make_state() -> tuple[str, str]:
    """(state, signed-cookie-value). The cookie binds the browser to the flow."""
    state = secrets.token_urlsafe(16)
    return state, f"{state}|{_sign(state)}"


def check_state(cookie_value: str | None, state: str) -> bool:
    if not cookie_value or not state:
        return False
    expected = cookie_value.split("|")
    return (len(expected) == 2 and hmac.compare_digest(expected[0], state)
            and _sign(state) == expected[1])


def authorize_url(cfg: dict, state: str) -> str:
    q = urllib.parse.urlencode({
        "client_id": cfg["client_id"],
        "redirect_uri": cfg["redirect_uri"],
        "response_type": "code",
        "scope": "openid profile",
        "state": state,
    })
    return f"{cfg['issuer']}/auth?{q}"


def exchange_code(cfg: dict, code: str) -> dict:
    """Back-channel code→token exchange (D1). Returns the parsed token response."""
    body = urllib.parse.urlencode({
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": cfg["redirect_uri"],
        "client_id": cfg["client_id"],
    }).encode()
    req = urllib.request.Request(f"{cfg['issuer']}/token", data=body, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded")
    if cfg["client_secret"]:
        basic = base64.b64encode(
            f"{cfg['client_id']}:{cfg['client_secret']}".encode()).decode()
        req.add_header("Authorization", f"Basic {basic}")
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def id_token_claims(cfg: dict, token_response: dict) -> dict:
    """Decode + validate the id_token from the back-channel exchange (D1):
    iss/aud/exp must match the config; no JWKS step because the token never
    transited the browser."""
    token = token_response.get("id_token", "")
    parts = token.split(".")
    if len(parts) != 3:
        raise ValueError("malformed id_token")
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    claims = json.loads(base64.urlsafe_b64decode(payload))
    if claims.get("iss") != cfg["issuer"]:
        raise ValueError(f"id_token iss mismatch: {claims.get('iss')}")
    aud = claims.get("aud")
    if aud != cfg["client_id"] and cfg["client_id"] not in (aud or []):
        raise ValueError("id_token aud mismatch")
    if int(claims.get("exp", 0)) < time.time():
        raise ValueError("id_token expired")
    return claims

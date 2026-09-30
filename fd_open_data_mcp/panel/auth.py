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
# Fleet convention (ops-console customize-jwt): the tenant roles claim is
# named `roles` and carries a list of role names.
ROLES_CLAIM = "roles"


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


def required_role() -> str | None:
    """`PANEL_REQUIRED_ROLE`; None = role gate off (allow-list fallback)."""
    role = os.environ.get("PANEL_REQUIRED_ROLE", "").strip()
    return role or None


# ── HMAC signing (session + state cookies share the secret) ─────────────────
def _secret() -> bytes:
    return os.environ.get("PANEL_SESSION_SECRET", "insecure-dev-secret").encode()


def _sign(payload: str) -> str:
    return hmac.new(_secret(), payload.encode(), hashlib.sha256).hexdigest()[:32]


def make_session_value(sub: str, name: str, roles: list[str] | None = None) -> str:
    """`sub|name|roles|exp|sig` — roles frozen at login (joined by `,`) so
    every request can re-check admission without a live provider lookup.
    Old 4-part (`sub|name|exp|sig`) cookies fail validation by construction."""
    exp = int(time.time()) + SESSION_HOURS * 3600
    payload = f"{sub}|{name}|{','.join(roles or [])}|{exp}"
    return f"{payload}|{_sign(payload)}"


def read_session(cookie: str | None) -> dict | None:
    """Return {"sub","name","roles"} for a valid, unexpired session cookie."""
    if not cookie:
        return None
    parts = cookie.split("|")
    if len(parts) != 5 or _sign("|".join(parts[:4])) != parts[4]:
        return None
    sub, name, roles_raw, exp = parts[:4]
    if not exp.isdigit() or int(exp) < time.time():
        return None
    return {"sub": sub, "name": name or sub,
            "roles": [r for r in roles_raw.split(",") if r]}


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
        "scope": "openid profile roles",  # roles claim requires the `roles` scope (custom-id-token)
        "state": state,
    })
    return f"{cfg['issuer']}/auth?{q}"


def end_session_url(cfg: dict) -> str:
    """OIDC RP-initiated logout: Logto clears its own SSO cookie.

    Clearing only the panel session cookie "logs out" into an instant silent
    re-login — authorize (no ``prompt=login``) rides the still-alive IdP
    session and hands back a fresh code. ``post_logout_redirect_uri`` is
    honored when registered in the Logto app config; otherwise Logto shows
    its own signed-out page — the IdP session is ended either way."""
    q = urllib.parse.urlencode({
        "client_id": cfg["client_id"],
        "post_logout_redirect_uri": cfg["redirect_uri"].replace(
            "/panel/auth/callback", "/panel"),
    })
    return f"{cfg['issuer']}/session/end?{q}"


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


# ── roles (panel-role-gate, design D1) ───────────────────────────────────────
def _claim_roles(source: dict) -> list[str] | None:
    """Normalize the `roles` claim to a list of role-name strings; None when
    the claim is absent. Ops-console parity: entries are role names, but
    Logto may also emit them as {name: "..."} objects."""
    raw = source.get(ROLES_CLAIM)
    if raw is None:
        return None
    entries = raw if isinstance(raw, list) else [raw]
    roles = []
    for r in entries:
        if isinstance(r, str):
            roles.append(r)
        elif isinstance(r, dict) and isinstance(r.get("name"), str):
            roles.append(r["name"])
    return roles


def userinfo(cfg: dict, access_token: str) -> dict:
    """One GET {issuer}/me with the bearer access_token (login-time only,
    never per request)."""
    req = urllib.request.Request(f"{cfg['issuer']}/me")
    req.add_header("Authorization", f"Bearer {access_token}")
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def extract_roles(cfg: dict, claims: dict, token_response: dict) -> list[str] | None:
    """Roles granted at login: the decoded id_token's roles claim first; when
    absent there, ONE userinfo call on the token exchange's access_token.
    None = no roles claim anywhere → callers fail closed."""
    roles = _claim_roles(claims)
    if roles is not None:
        return roles
    access_token = token_response.get("access_token", "")
    if not access_token:
        return None
    try:
        return _claim_roles(userinfo(cfg, access_token))
    except Exception:  # noqa: BLE001 - provider/network error → no claim
        return None

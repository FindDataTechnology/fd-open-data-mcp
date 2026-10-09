"""Auth / login-station / proxy group dictionary i18n smoke tests
(panel-rbac-i18n-refresh task 4.3).

Renders the auth console, its polled partial, the proxy page and the circuit
partial under the ``panel-lang=en`` cookie and asserts the English dictionary
surface (headings, queue chrome, badges, egress picker, observation modal
copy, proxy forms), plus the zh default staying intact. Also guards that
every ``t('…')``/``tr('…')`` key used by the group's templates resolves in
the merged dictionary.
"""
from __future__ import annotations

import datetime as dt
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from fd_open_data_mcp.db import get_database
from fd_open_data_mcp.models import (
    CrawlIdentity, CrawlLoginStation, CrawlSite, CrawlSource, Proxy,
)
from fd_open_data_mcp.panel import i18n
from fd_open_data_mcp.panel.app import app

client = TestClient(app)

TEMPLATES_DIR = Path(__file__).parent.parent / "fd_open_data_mcp" / "panel" / "templates"
GROUP_TEMPLATES = (
    "auth.html", "partial_auth.html", "_auth_alert.html",
    "_station_row.html", "_station_view.html", "_identity_created.html",
    "proxy.html", "partial_proxy.html",
)


@pytest.fixture
def open_env(monkeypatch):
    for k in ("LOGTO_ISSUER", "LOGTO_CLIENT_ID", "LOGTO_CLIENT_SECRET",
              "PANEL_TOKEN", "PROXY_CONTROL_URL"):
        monkeypatch.delenv(k, raising=False)


def _seed_source(name="rmfyalk", auth_profile="rmfyalk-login") -> None:
    """Same seed shape as tests/test_panel_station.py (direct model writes —
    the panel never writes the identity tables)."""
    s = get_database().get_session()
    try:
        if s.get(CrawlSite, "tencent") is None:
            s.add(CrawlSite(id="tencent", enabled=True))
        if s.get(CrawlSource, name) is None:
            s.add(CrawlSource(source=name, site="tencent",
                              schedule="0 6 * * *", enabled=True,
                              auth_profile=auth_profile))
        s.commit()
    finally:
        s.close()


def _seed_identity(source, alias, status) -> None:
    s = get_database().get_session()
    try:
        s.add(CrawlIdentity(source=source, account_alias=alias, status=status,
                            automation="assisted"))
        s.commit()
    finally:
        s.close()


def _seed_proxy(ip="10.0.0.9") -> None:
    s = get_database().get_session()
    try:
        s.add(Proxy(scheme="http", ip=ip, port=8080, auth="u:sekret",
                    status="active", label="gost-xinru3",
                    provider="paid-static"))
        s.commit()
    finally:
        s.close()


def _seed_station(source, alias, status="waiting_operator") -> int:
    """A live station (deadline the timeout backstop honours as live)."""
    s = get_database().get_session()
    try:
        row = CrawlLoginStation(
            identity_id=1, source=source, account_alias=alias, status=status,
            proxy_url="http://u:p@1.2.3.4:8080",
            deadline_at=dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=25))
        s.add(row)
        s.commit()
        return row.id
    finally:
        s.close()


def _en(path, **kwargs):
    return client.get(path, cookies={"panel-lang": "en"}, **kwargs)


def _norm(html):
    return re.sub(r"\s+", " ", html)


# ── dictionary hygiene ───────────────────────────────────────────────────────

def test_group_file_loaded_and_every_template_key_resolves():
    """i18n.py merged i18n_en_auth.EN_ENTRIES, and every ``t('…')``/``tr('…')``
    key the group's templates use resolves to a translation — no silent zh
    fallback from a typo'd key (core nav/button words like 登录/操作/状态 are
    expected to resolve through the shared core dictionary)."""
    from fd_open_data_mcp.panel.i18n_en_auth import EN_ENTRIES

    assert EN_ENTRIES  # non-empty group file
    assert i18n.EN.get("拉起登录站") == "Launch login station"
    assert i18n.EN.get("每 {n}s 自动刷新。") == "Refreshes every {n}s."

    used: set[str] = set()
    for name in GROUP_TEMPLATES:
        text = (TEMPLATES_DIR / name).read_text(encoding="utf-8")
        used |= set(re.findall(r"\bt\('([^']+)'\)", text))
        used |= set(re.findall(r"\btr\('([^']+)'\)", text))
    assert used, "template key extraction found nothing — regex drifted"
    missing = sorted(k for k in used if k not in i18n.EN)
    assert not missing, f"t()/tr() keys missing from the dictionary: {missing}"


def test_group_entries_are_wellformed():
    from fd_open_data_mcp.panel.i18n_en_auth import EN_ENTRIES
    for k, v in EN_ENTRIES.items():
        assert isinstance(k, str) and k.strip(), repr(k)
        assert isinstance(v, str), repr((k, v))


# ── en render smoke: auth shell + polled partial ────────────────────────────

def test_auth_page_en(session, open_env):
    _seed_source()
    _seed_identity("rmfyalk", "acct001", "login_required")
    _seed_identity("rmfyalk", "acct002", "active")  # never probed -> stale

    r = _en("/panel/auth")
    assert r.status_code == 200
    t = _norm(r.text)
    # page headline + short briefs through the dictionary ({n} interpolated)
    assert "Auth identities" in t
    assert "Per-source multi-account identity pool" in t
    assert "completed inside the embedded observation view" in t
    assert "Refreshes every 15s." in t
    # the standing alert banner renders monolingual English (count-neutral
    # phrasing keeps 1/N grammatical) and points at the queue buttons
    assert "identities needing a login: 1" in t
    assert "identities with possibly expired sessions: 1" in t
    assert "use the Sign in buttons below to open a login station" in t
    # zh chrome must not bleed onto the en page
    assert "认证身份池" not in t and "需登录队列" not in t


def test_auth_partial_en(session, open_env):
    _seed_source()
    _seed_proxy()                       # -> the egress picker renders
    _seed_identity("rmfyalk", "q-a", "login_required")
    _seed_station("rmfyalk", "q-a")     # -> a live station row (adapter path)

    r = _en("/panel/partials/auth")
    assert r.status_code == 200 and "<html" not in r.text
    t = _norm(r.text)
    # section headings + summary badges
    assert "Login-required queue" in t
    assert "Identity matrix" in t and "Identities 1" in t
    assert "Login required 1" in t
    # the queue row's visible launch entry + the egress picker options
    assert "Launch login station" in t
    assert "Egress: keep current (unbound)" in t
    assert "Egress: auto" in t
    # the never-logged-in queue cell + empty states
    assert "never logged in" in t
    assert "No identity events yet." in t
    assert "latest 20" in t
    # the station board row renders through the tr() adapter (two-step
    # reclaim) — the include path where `t` IS the translate helper
    assert "Observation view" in t
    assert "Confirm reclaim" in t and ">Keep<" in t
    # zh chrome must not bleed onto the en partial
    assert "需登录队列" not in t and "拉起登录站" not in t
    assert "观察窗" not in t and "确认回收" not in t


def test_station_view_modal_en(session, open_env):
    """The observation modal's static copy translates; the relaunch form of a
    finished station carries the dictionary too."""
    _seed_source()
    _seed_identity("rmfyalk", "acct-v", "login_required")
    s = get_database().get_session()
    try:
        row = CrawlLoginStation(
            identity_id=1, source="rmfyalk", account_alias="acct-v",
            status="failed", proxy_url="http://u:p@1.2.3.4:8080",
            note="Error: boom",
            created_at=dt.datetime.now(dt.timezone.utc),
            finished_at=dt.datetime.now(dt.timezone.utc))
        s.add(row)
        s.commit()
        sid = row.id
    finally:
        s.close()

    r = _en(f"/panel/auth/station/{sid}/view")
    assert r.status_code == 200
    t = _norm(r.text)
    assert "Sign in ·" in t                     # modal headline
    assert "Failure reason: Error: boom" in t   # static reason line
    assert "Relaunch login station" in t        # the retry button
    # the poller script's operator copy ships through the dictionary
    # (tojson keep_ascii-escapes the em dash, so match around it)
    assert "Login captured" in r.text
    assert "complete the login above." in r.text
    assert "画面已就绪" not in r.text


def test_identity_created_fragment_en(session, open_env):
    """The registration confirmation banner (_identity_created) swaps in
    monolingual English under the en cookie."""
    _seed_source()
    _seed_proxy()
    r = client.post("/panel/auth/identities",
                    cookies={"panel-lang": "en"},
                    data={"source": "rmfyalk", "account_alias": "acc-en"},
                    headers={"HX-Request": "true"})
    assert r.status_code == 200
    t = _norm(r.text)
    assert "Registered:" in t
    assert "Egress" in t and "login_required" in t
    assert "已登记" not in t


# ── en render smoke: proxy page + circuit partial ────────────────────────────

def test_proxy_page_en(session, open_env):
    r = _en("/panel/proxy")
    assert r.status_code == 200
    t = _norm(r.text)
    assert "Proxy / Egress" in t
    assert "Egress health (per source × proxy)" in t
    assert "Cold-table snapshot (near-realtime)" in t
    # forms + tables (management unset in tests -> the read-only banner)
    assert "Management actions are disabled" in t
    assert "This page is read-only" in t
    assert "Import static endpoints" in t and "Provider name" in t
    assert "Per-source rate limits" in t and "Force circuit reset" in t
    assert "Ban rules (read-only)" in t
    # empty states through the dictionary
    assert "No provider-owned proxy rows." in t
    assert "No proxy rows." in t and "No ban rules registered." in t
    # zh chrome must not bleed onto the en page
    assert "代理与出口" not in t and "出口健康" not in t

    part = _norm(_en("/panel/partials/proxy").text)
    assert "Fail streak" in part and "Cooldown" in part
    assert "No circuit state recorded yet." in part


# ── zh default intact ────────────────────────────────────────────────────────

def test_auth_and_proxy_pages_zh_untouched(session, open_env):
    _seed_source()
    _seed_identity("rmfyalk", "acct001", "login_required")

    auth = _norm(client.get("/panel/auth").text)
    assert "认证身份池" in auth
    assert "登录经登录站完成" in auth          # brief item stays verbatim zh
    assert "每 15s 自动刷新" in auth
    assert "个身份需登录" in auth              # standing banner in zh

    part = _norm(client.get("/panel/partials/auth").text)
    assert "需登录队列" in part and "拉起登录站" in part
    assert "身份矩阵" in part and "最近 20 条" in part

    proxy = _norm(client.get("/panel/proxy").text)
    assert "代理与出口" in proxy and "出口健康" in proxy
    assert "本页面为只读" in proxy

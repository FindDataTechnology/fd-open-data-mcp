"""Panel authenticated-crawling tests (session-pool 4.1).

Covers the auth panel: the static shell (polling + the read-only note —
logins happen on the login site), the polled partial's identity matrix
grouped by source with five-state badges + the lease view, the
login-required queue, the latest-20 event stream, empty-state copy, partial
degradation, the token gate, and the source-detail surface (authenticated
badge for auth_profile sources + the leased identity_alias on run rows).
All data is seeded directly through the models on the per-test sqlite
fixture — the panel never writes the identity tables.
"""
from __future__ import annotations

import datetime as dt

from fastapi.testclient import TestClient

from fd_open_data_mcp.db import get_database
from fd_open_data_mcp.models import (
    CrawlIdentity, CrawlIdentityEvent, CrawlRun, CrawlSite, CrawlSource,
)
from fd_open_data_mcp.panel.app import app

client = TestClient(app)

NOW = dt.datetime.utcnow()
BASE = NOW - dt.timedelta(days=7)


# ── seed helpers ────────────────────────────────────────────────────────────
def _source(name, site="tencent", auth_profile=None) -> str:
    s = get_database().get_session()
    try:
        if s.get(CrawlSite, site) is None:
            s.add(CrawlSite(id=site, enabled=True))
        if s.get(CrawlSource, name) is None:
            s.add(CrawlSource(source=name, site=site, schedule="0 6 * * *",
                              enabled=True, auth_profile=auth_profile))
        s.commit()
        return name
    finally:
        s.close()


def _ident(source, alias, status="active", automation="assisted", **kw) -> int:
    s = get_database().get_session()
    try:
        row = CrawlIdentity(source=source, account_alias=alias, status=status,
                            automation=automation, **kw)
        s.add(row)
        s.commit()
        return row.id
    finally:
        s.close()


def _event(identity_id, kind, detail=None, lease_token=None,
           created=None) -> int:
    s = get_database().get_session()
    try:
        row = CrawlIdentityEvent(identity_id=identity_id, kind=kind,
                                 detail=detail, lease_token=lease_token,
                                 created_at=created or NOW)
        s.add(row)
        s.commit()
        return row.id
    finally:
        s.close()


def _seed_run(source, status="success", *, identity_alias=None) -> int:
    s = get_database().get_session()
    try:
        r = CrawlRun(source=source, status=status, started_at=NOW,
                     finished_at=NOW, identity_alias=identity_alias)
        s.add(r)
        s.commit()
        return r.id
    finally:
        s.close()


def _matrix() -> str:
    """The partial's identity-matrix region (between its h3 and the events)."""
    text = client.get("/panel/partials/auth").text
    return text.split("身份矩阵", 1)[1].split("事件流", 1)[0]


# ── 4.1 page shell: polling, station-launch note, nav ────────────────────────
def test_auth_page_shell_polling_and_station_note(session):
    page = client.get("/panel/auth").text
    assert "认证身份池" in page
    # the shell carries the polled region; the page queries only the standing
    # alert summary (prearm-login-station: the banner must be visible on the
    # first paint, before any partial swap) — everything else polls
    assert 'hx-get="/panel/partials/auth"' in page
    assert "每 15s" in page
    # logins happen on a login station launched FROM this panel, completed in
    # the embedded observation view (login-station-console 3.3 replaced the
    # old read-only note) — monolingual since panel-rbac-i18n-refresh 4.3
    # (en wording covered by tests/test_i18n_auth.py)
    assert "登录经登录站完成" in page
    assert "观察窗" in page
    # the station observation view swaps into this page-side region
    assert 'id="station-view"' in page
    # nav entry visible from the home cockpit
    assert 'href="/panel/auth"' in client.get("/panel").text


def test_auth_routes_behind_token_gate(session, monkeypatch):
    monkeypatch.setenv("PANEL_TOKEN", "sekret")
    from importlib import reload

    import fd_open_data_mcp.panel.app as appmod

    gated = TestClient(reload(appmod).app)
    assert gated.get("/panel/auth").status_code == 401
    assert gated.get("/panel/partials/auth").status_code == 401
    ok = {"X-Panel-Token": "sekret"}
    assert gated.get("/panel/auth", headers=ok).status_code == 200
    assert gated.get("/panel/partials/auth", headers=ok).status_code == 200
    monkeypatch.undo()
    reload(appmod)


# ── 4.1 identity matrix ──────────────────────────────────────────────────────
def test_auth_partial_identity_matrix_grouped_by_source(session):
    _source("rmfyalk", auth_profile="rmfyalk-login")
    _source("law")
    i1 = _ident("rmfyalk", "acc-a", "active", automation="auto",
                last_login_at=BASE + dt.timedelta(days=1),
                last_success_at=BASE + dt.timedelta(days=1),
                lease_owner="dispatch-1", lease_token="tok-1234567890",
                lease_expires_at=NOW + dt.timedelta(hours=1))
    _ident("rmfyalk", "acc-b", "login_required", failure_count=3)
    _ident("law", "acc-x", "banned", consecutive_zero_runs=5)

    matrix = _matrix()
    # grouped by source, groups sorted by name: law before rmfyalk
    assert matrix.index("law") < matrix.index("rmfyalk")
    for marker in ("acc-a", "acc-b", "acc-x", "auto", "assisted"):
        assert marker in matrix
    # health facts per row: lease held by its owner, zero-run streak surfaced
    assert "租借中" in matrix and "dispatch-1" in matrix
    assert ">5<" in matrix  # consecutive_zero_runs of the banned identity
    # the authenticated source's group header carries its badge
    assert "认证源" in matrix

    # the same facts come back from the shared aggregation (partial + tools)
    from fd_open_data_mcp.visibility import snapshot

    s = get_database().get_session()
    try:
        rows = {r["account_alias"]: r for r in snapshot.identity_pool(s)}
    finally:
        s.close()
    assert rows["acc-a"]["leased"] is True
    assert rows["acc-a"]["lease_expired"] is False
    assert rows["acc-b"]["failure_count"] == 3


def test_auth_partial_status_badges_five_states(session):
    _source("five")
    _ident("five", "i-login", "login_required")
    _ident("five", "i-active", "active")
    _ident("five", "i-cool", "cooldown")
    _ident("five", "i-ban", "banned")
    _ident("five", "i-retired", "retired")

    matrix = _matrix()
    for alias, cls in (("i-login", "refused"), ("i-active", "success"),
                       ("i-cool", "running"), ("i-ban", "failed"),
                       ("i-retired", "cancelled")):
        row = matrix.split(alias, 1)[1].split("</tr>", 1)[0]
        assert f'badge {cls}"' in row, alias


def test_auth_partial_lease_expired_vs_held(session):
    _source("leases")
    _ident("leases", "held", "active", lease_owner="w1",
           lease_expires_at=NOW + dt.timedelta(minutes=30))
    _ident("leases", "stale", "active", lease_owner="w2",
           lease_token="tok-w2", lease_expires_at=NOW - dt.timedelta(minutes=5))

    matrix = _matrix()
    held_row = matrix.split(">held<", 1)[1].split("</tr>", 1)[0]
    stale_row = matrix.split(">stale<", 1)[1].split("</tr>", 1)[0]
    assert "租借中" in held_row and "租约过期" not in held_row
    assert "租约过期" in stale_row and "租借中" not in stale_row


# ── 4.1 login-required queue ────────────────────────────────────────────────
def test_auth_partial_login_required_queue(session):
    _source("q-src")
    _ident("q-src", "needs-login", "login_required",
           last_login_at=BASE, failure_count=4)
    _ident("q-src", "fresh-reg", "login_required")            # never logged in
    _ident("q-src", "healthy", "active")                      # not queued
    _ident("q-src", "banned-one", "banned")                   # not queued

    r = client.get("/panel/partials/auth")
    assert r.status_code == 200 and "<html" not in r.text
    queue_region = r.text.split("需登录队列", 1)[1].split("身份矩阵", 1)[0]
    assert "needs-login" in queue_region and "fresh-reg" in queue_region
    assert "healthy" not in queue_region and "banned-one" not in queue_region
    assert ">4<" in queue_region            # failure_count surfaced
    assert "从未登录" in queue_region
    # most-failed first: needs-login (4) ahead of fresh-reg (0)
    assert queue_region.index("needs-login") < queue_region.index("fresh-reg")
    # summary chips carry the queue length
    assert "需登录 2" in r.text


def test_auth_partial_empty_database(session):
    r = client.get("/panel/partials/auth")
    assert r.status_code == 200 and "<html" not in r.text
    for empty in ("队列为空", "尚无身份", "尚无事件"):
        assert empty in r.text
    assert "身份 0" in r.text


# ── 4.1 event stream ────────────────────────────────────────────────────────
def test_auth_partial_event_stream_latest_20_with_kind_colors(session):
    _source("ev-src")
    ident = _ident("ev-src", "acc-a", "active")
    for i in range(1, 26):  # 25 events, created_at strictly increasing
        _event(ident, "note" if i % 3 else "auth_failed",
               detail=f"ev-{i:02d}", created=BASE + dt.timedelta(minutes=i))

    r = client.get("/panel/partials/auth")
    events_region = r.text.split("事件流", 1)[1]
    assert "ev-25" in events_region and "ev-06" in events_region  # newest 20
    assert "ev-05" not in events_region and "ev-01" not in events_region  # cut
    assert "最近 20 条" in r.text
    # newest first
    assert events_region.index("ev-25") < events_region.index("ev-24")
    # kind coloring: the auth_failed row carries the failure badge, note the
    # neutral one (the badge cell precedes the kind text inside each <tr>)
    assert "badge failed" in events_region.split("auth_failed")[0].split("<tr>")[-1]
    assert "badge no_op" in events_region.split(">note<")[0].split("<tr>")[-1]
    # the identity is resolvable on every row (source link + alias)
    assert "ev-src" in events_region and "acc-a" in events_region


def test_auth_partial_degrades_like_every_home_partial(session):
    from unittest.mock import patch

    from fd_open_data_mcp.visibility import snapshot

    with patch.object(snapshot, "identity_pool",
                      side_effect=RuntimeError("identity db gone")):
        down = client.get("/panel/partials/auth")
    assert down.status_code == 200
    assert "section unavailable" in down.text
    assert "identity db gone" in down.text


# ── 4.1 source-detail surface ───────────────────────────────────────────────
def test_source_detail_authenticated_badge(session):
    _source("authed-src", auth_profile="rmfyalk-login")
    _source("anon-src", auth_profile=None)

    page = client.get("/panel/sources/authed-src").text
    assert "认证源 authenticated" in page
    assert "rmfyalk-login" in page
    assert 'href="/panel/auth"' in page          # drill into the auth panel
    assert "认证源" not in client.get("/panel/sources/anon-src").text


def test_source_detail_runs_show_identity_alias(session):
    _source("alias-src", auth_profile="p")
    _ident("alias-src", "acc-1", "active")
    _seed_run("alias-src", "success", identity_alias="acc-1")
    _seed_run("alias-src", "success", identity_alias=None)  # anonymous run

    page = client.get("/panel/sources/alias-src").text
    assert "acc-1" in page                        # leased identity surfaced
    rows = page.split("最近 20 次运行", 1)[1].split("</table>", 1)[0]
    assert rows.count("acc-1") == 1               # only the run that used it

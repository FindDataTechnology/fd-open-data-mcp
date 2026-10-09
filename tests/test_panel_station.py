"""Panel login-station console tests (login-station-console 3.2/3.3/3.4).

Covers the panel surface: the launch route (identity ensured + station
launched + iframe-embedded observation view swapped in), the noVNC HTTP
reverse proxy and the websocket relay against local mock upstreams, the token
gate over the observation channel (spec: 观察通道不裸奔), the multi-account
registration form, the station board (badges + two-step reclaim), the
station-view fragment, and the auth_launch_login MCP tool end-to-end on a
FakeStationClient (shared with test_station_ops).
"""
from __future__ import annotations

import asyncio
import datetime as dt
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import fd_open_data_mcp.panel.app as appmod
from fd_open_data_mcp import station_ops
from fd_open_data_mcp.db import get_database
from fd_open_data_mcp.models import CrawlIdentity, CrawlLoginStation
from fd_open_data_mcp.panel.app import app
from test_station_ops import (  # shared doubles + seed helpers (same dir)
    NOW, FakeStationClient, _ident, _proxy, _source, _station,
)

client = TestClient(app)

# A deadline the timeout backstop will honour as live: station_status compares
# against the real clock, so the frozen NOW fixture (2026-09-26) reads as long
# overdue and every "live" fixture silently becomes a timeout row.
LIVE_DEADLINE = dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=25)


def _db():
    return get_database().get_session()


# ── token gate: the observation channel never bypasses panel auth ───────────
def test_station_routes_behind_token_gate(session, monkeypatch):
    monkeypatch.setenv("PANEL_TOKEN", "sekret")
    from importlib import reload

    gated = TestClient(reload(appmod).app)
    try:
        # no credential -> refused (401; the channel and the writes)
        assert gated.get("/panel/auth/station/1/vnc/vnc.html").status_code == 401
        assert gated.post("/panel/auth/station/launch",
                          data={"source": "s", "account_alias": "a"}
                          ).status_code == 401
        assert gated.post("/panel/auth/identities",
                          data={"source": "s", "account_alias": "a"}
                          ).status_code == 401
        # the OIDC endpoints stay public
        assert gated.get("/panel/auth/whoami").status_code == 200
        # with the token the request passes the gate (then 404s: no station 42)
        ok = {"X-Panel-Token": "sekret"}
        assert gated.get("/panel/auth/station/424242/vnc/vnc.html",
                         headers=ok).status_code == 404
    finally:
        monkeypatch.undo()
        reload(appmod)


# ── launch route: ensure + launch + embedded view ───────────────────────────
def test_station_launch_route_launches_and_embeds_view(session, monkeypatch):
    _source("rmfyalk", auth_profile="rmfyalk-login")
    _proxy(ip="10.0.0.1")
    fake = FakeStationClient()
    monkeypatch.setattr(appmod, "_station_client", lambda: fake)

    r = client.post("/panel/auth/station/launch",
                    data={"source": "rmfyalk", "account_alias": "acc-a"},
                    headers={"hx-request": "true"})

    assert r.status_code == 200
    # the observation view embeds the panel-relative noVNC page (no token in it)
    assert 'src="/panel/auth/station/1/vnc/vnc.html?autoconnect=1' in r.text
    assert "path=websockify" in r.text
    assert "token=" not in r.text
    # the partial is told to refresh + a toast announces the launch
    trigger = r.headers.get("hx-trigger", "")
    assert "auth-refresh" in trigger and "toast" in trigger
    # one Job + one Service were created
    assert len(fake.manifests("Job")) == 1
    assert len(fake.manifests("Service")) == 1
    s = _db()
    try:
        st = s.get(CrawlLoginStation, 1)
        assert st.status == "waiting_operator"
        ident = s.query(CrawlIdentity).filter_by(
            source="rmfyalk", account_alias="acc-a").one()
        assert ident.status == "login_required"
        assert ident.egress_ref == "proxy:1"
    finally:
        s.close()


def test_station_launch_route_plain_post_redirects(session, monkeypatch):
    _source("rmfyalk")
    fake = FakeStationClient()
    monkeypatch.setattr(appmod, "_station_client", lambda: fake)
    r = client.post("/panel/auth/station/launch",
                    data={"source": "rmfyalk", "account_alias": "acc-p"},
                    follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"].endswith("/panel/auth")


def test_station_launch_route_missing_fields(session):
    r = client.post("/panel/auth/station/launch", data={"source": ""},
                    headers={"hx-request": "true"})
    assert r.status_code == 400


# ── operator-chosen egress (the "I can't pick my proxy" defect) ──────────────
def test_launch_route_honours_explicit_egress(session, monkeypatch):
    _source("rmfyalk", auth_profile="rmfyalk-login")
    p_auto = _proxy(ip="10.0.0.1")
    p_pick = _proxy(ip="10.0.0.2", label="gost-cheap")
    fake = FakeStationClient()
    monkeypatch.setattr(appmod, "_station_client", lambda: fake)

    r = client.post("/panel/auth/station/launch",
                    data={"source": "rmfyalk", "account_alias": "acc-a",
                          "egress_ref": f"proxy:{p_pick}"},
                    headers={"hx-request": "true"})

    assert r.status_code == 200
    s = _db()
    try:
        ident = s.query(CrawlIdentity).filter_by(
            source="rmfyalk", account_alias="acc-a").one()
        assert ident.egress_ref == f"proxy:{p_pick}"   # the picked one
        st = s.get(CrawlLoginStation, 1)
        assert f"10.0.0.2:8080" in (st.proxy_url or "")
    finally:
        s.close()


def test_launch_route_rejects_bad_egress_with_reason(session, monkeypatch):
    _source("rmfyalk", auth_profile="rmfyalk-login")
    fake = FakeStationClient()
    monkeypatch.setattr(appmod, "_station_client", lambda: fake)

    r = client.post("/panel/auth/station/launch",
                    data={"source": "rmfyalk", "account_alias": "acc-a",
                          "egress_ref": "proxy:424242"},
                    headers={"hx-request": "true"})

    assert r.status_code == 200          # a toast, not a 500
    assert "unknown or retired" in r.headers.get("hx-trigger", "")
    assert fake.created == []            # nothing was launched


def test_partial_renders_egress_picker(session):
    _source("rmfyalk", auth_profile="rmfyalk-login")
    pid = _proxy(ip="10.0.0.3", auth="u:sekret", label="gost-xinru3")
    iid = _ident("rmfyalk", "acc-a", "login_required")
    r = client.get("/panel/partials/auth")
    assert r.status_code == 200
    # picker exists with an auto default + the masked choice
    assert 'name="egress_ref"' in r.text
    assert "egress: auto" in r.text or "egress: keep current" in r.text
    assert "gost-xinru3" in r.text and f"proxy:{pid}" in r.text
    assert "u:sekret" not in r.text      # credentials never render


# ── standing auth alert banner (prearm-login-station piece 3) ───────────────
def test_auth_page_shows_standing_alert_banner(session, monkeypatch):
    """A login-required identity plus a stale active one must produce a
    STANDING banner on /panel/auth — not a transient toast: the operator who
    was asleep when the session died still sees it at a glance."""
    _source("rmfyalk", auth_profile="rmfyalk-login")
    _ident("rmfyalk", "acct001", "login_required")
    _ident("rmfyalk", "acct002", "active")   # never probed -> stale

    monkeypatch.setenv("PANEL_TOKEN", "sekret")
    from importlib import reload

    gated = TestClient(reload(appmod).app)
    try:
        ok = {"X-Panel-Token": "sekret"}
        page = gated.get("/panel/auth", headers=ok)
        assert page.status_code == 200
        assert "1 个身份需登录" in page.text
        assert "1 个身份会话可能已过期" in page.text
        assert "点下方「登录 login」拉起登录站" in page.text
        # the English second line rides along
        assert "may have an expired session" in page.text
        # the polled partial carries the same banner (it stays current)
        part = gated.get("/panel/partials/auth", headers=ok)
        assert part.status_code == 200
        assert "1 个身份需登录" in part.text
        # banner sits at the top, ahead of the summary badges
        assert part.text.index("个身份需登录") < part.text.index("身份 identities")
    finally:
        monkeypatch.undo()
        reload(appmod)


def test_auth_page_has_no_alert_banner_when_all_healthy(session):
    _source("rmfyalk", auth_profile="rmfyalk-login")
    _ident("rmfyalk", "acc-a", "active",
           last_probe_at=dt.datetime.now(dt.timezone.utc))
    r = client.get("/panel/auth")
    assert r.status_code == 200
    assert "个身份需登录" not in r.text
    assert "个身份会话可能已过期" not in r.text
    assert client.get("/panel/partials/auth").text.count("个身份需登录") == 0


# ── background 预拉起 pre-arm loop ──────────────────────────────────────────
def test_prearm_loop_runs_a_pass_and_continues(session, monkeypatch):
    """The loop must call prearm_tick every interval, log, and survive a
    failing pass (a cluster hiccup must never kill the background task)."""
    calls: list[int] = []

    def fake_tick(s, now=None, client=None):
        calls.append(len(calls) + 1)
        if len(calls) == 1:
            raise RuntimeError("cluster hiccup")   # first pass fails
        return [{"source": "rmfyalk", "account_alias": "acct001",
                 "station_id": 1, "status": "launched"}]

    monkeypatch.setattr(station_ops, "prearm_tick", fake_tick)
    monkeypatch.setattr(station_ops, "prearm_interval_seconds", lambda: 0)
    monkeypatch.setenv("FD_PREARM_ENABLED", "1")

    async def run() -> None:
        task = asyncio.get_running_loop().create_task(appmod._prearm_loop())
        for _ in range(200):
            await asyncio.sleep(0.01)
            if len(calls) >= 2:
                break
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    assert len(calls) >= 2


def test_prearm_loop_disabled_by_env(monkeypatch):
    monkeypatch.setenv("FD_PREARM_ENABLED", "0")
    called = []
    monkeypatch.setattr(station_ops, "prearm_tick",
                        lambda *a, **k: called.append(1) or [])
    asyncio.run(appmod._prearm_loop())     # returns immediately, never loops
    assert called == []


# ── new-account form ────────────────────────────────────────────────────────
def test_new_account_form_registers_identity_with_egress(session):
    _source("rmfyalk", auth_profile="rmfyalk-login")
    _source("anon", auth_profile=None)
    pid = _proxy(ip="10.0.0.2")

    r = client.post("/panel/auth/identities",
                    data={"source": "rmfyalk", "account_alias": "acc-new"},
                    headers={"hx-request": "true"})

    assert r.status_code == 200
    assert "acc-new" in r.text and f"proxy:{pid}" in r.text
    # proxy credentials never render
    assert "u:p" not in r.text
    s = _db()
    try:
        ident = s.query(CrawlIdentity).filter_by(
            source="rmfyalk", account_alias="acc-new").one()
        assert ident.status == "login_required"
        assert ident.egress_ref == f"proxy:{pid}"
    finally:
        s.close()


# ── the polled board ────────────────────────────────────────────────────────
def test_partial_shows_station_board_and_login_button(session):
    _source("rmfyalk", auth_profile="rmfyalk-login")
    iid = _ident("rmfyalk", "acc-a", "login_required")
    _ident("rmfyalk", "acc-b", "active")
    _station(iid, "rmfyalk", "acc-a", status="waiting_operator",
             deadline=LIVE_DEADLINE,
             proxy_url="http://user:pass@1.2.3.4:8080")

    r = client.get("/panel/partials/auth")
    assert r.status_code == 200 and "<html" not in r.text

    # login button ONLY on the login_required row of the matrix
    matrix = r.text.split("身份矩阵", 1)[1].split("事件流", 1)[0]
    row_a = matrix.split("acc-a", 1)[1].split("</tr>", 1)[0]
    row_b = matrix.split("acc-b", 1)[1].split("</tr>", 1)[0]
    assert "登录 login" in row_a
    assert "登录 login" not in row_b

    # the station board: row, badge, masked egress, two-step reclaim
    board = r.text.split("登录站", 2)[-1]
    assert "waiting_operator" in board and "#1" in board
    assert "确认回收 confirm" in board
    assert "user:pass" not in r.text          # credentials never render
    assert "••••" in board

    # the registration form lists only auth_profile sources, and the option
    # VALUE is the profile, not the crawl-source name: identities are keyed by
    # auth_profile and a crawl-source-named row is invisible to the dispatcher
    # (2026-10-09 fix — the seeded source here is rmfyalk / rmfyalk-login)
    assert 'value="rmfyalk-login"' in r.text
    form_region = r.text.split("新建账号", 1)[1].split("需登录队列", 1)[0]
    assert 'value="rmfyalk"' not in form_region
    assert "anon" not in form_region


def test_station_view_route_embeds_live_station_only(session):
    iid = _ident("rmfyalk", "acc-v")
    sid = _station(iid, "rmfyalk", "acc-v", status="waiting_operator",
                   deadline=LIVE_DEADLINE)
    r = client.get(f"/panel/auth/station/{sid}/view")
    assert r.status_code == 200
    assert f"/panel/auth/station/{sid}/vnc/vnc.html" in r.text

    # a completed station renders as a closed view (no iframe that could ever
    # connect), not a dead 404 — the operator needs the state, not an error
    done = _station(iid, "rmfyalk", "acc-v", status="completed")
    r2 = client.get(f"/panel/auth/station/{done}/view")
    assert r2.status_code == 200
    assert "vnc.html" not in r2.text
    # ...and an unknown station is still a friendly 404
    r3 = client.get("/panel/auth/station/999999/view")
    assert r3.status_code == 404
    assert "不可用" in r3.text or "unavailable" in r3.text


def test_station_view_shows_failure_reason_and_relaunch(session):
    """A failed station must tell the operator why and offer a retry: the
    reason lived only in the row's note, so 'failed' was a dead end."""
    _source("rmfyalk", auth_profile="rmfyalk-login")
    iid = _ident("rmfyalk", "acc-f", "login_required")
    sid = _station(iid, "rmfyalk", "acc-f", status="failed",
                   note=("Error: Page.goto: net::ERR_INVALID_AUTH_CREDENTIALS "
                         "at https://account.court.gov.cn/oauth/authorize\n"
                         "Call log:\n  - navigating to …"))
    r = client.get(f"/panel/auth/station/{sid}/view")
    assert r.status_code == 200
    assert "ERR_INVALID_AUTH_CREDENTIALS" in r.text
    # playwright's call log is noise, never rendered
    assert "navigating to" not in r.text
    assert "重新拉起登录站 relaunch" in r.text
    # the relaunch reuses the identity's source/alias
    assert 'name="source" value="rmfyalk"' in r.text
    assert 'name="account_alias" value="acc-f"' in r.text


# ── observation modal (2026-10-08 refactor) ─────────────────────────────────
def test_station_view_modal_headlines_site_and_account(session):
    """The modal must say WHICH site and WHICH account: a bare frame read as
    an opaque window and the operator had no idea what they were logging."""
    _source("rmfyalk", auth_profile="rmfyalk-login")
    iid = _ident("rmfyalk", "acct001", "login_required")
    sid = _station(iid, "rmfyalk", "acct001", status="waiting_operator",
                   deadline=LIVE_DEADLINE)
    r = client.get(f"/panel/auth/station/{sid}/view")
    assert r.status_code == 200
    # display name comes from the mapping, not the raw source code
    assert "人民法院案例库" in r.text
    assert "https://rmfyalk.court.gov.cn" in r.text
    # the account is a first-class headline element
    assert "<strong>acct001</strong>" in r.text
    # dialog semantics: backdrop + close button
    assert "station-backdrop" in r.text and "关闭" in r.text


def test_station_view_readiness_probe_uses_novnc_marker(session):
    """The boot probe must look for noVNC's real DOM marker: the previous
    size>200 probe matched the friendly 502 page and stopped retrying, so
    operators stared at '暂不可达' forever."""
    _source("rmfyalk", auth_profile="rmfyalk-login")
    iid = _ident("rmfyalk", "acc-m", "login_required")
    sid = _station(iid, "rmfyalk", "acc-m", status="waiting_operator",
                   deadline=LIVE_DEADLINE)
    r = client.get(f"/panel/auth/station/{sid}/view")
    assert "noVNC_screen" in r.text or "noVNC_container" in r.text
    assert "innerHTML.length > 200" not in r.text


def test_station_status_poll_endpoint(session):
    _source("rmfyalk", auth_profile="rmfyalk-login")
    iid = _ident("rmfyalk", "acc-p", "login_required")
    sid = _station(iid, "rmfyalk", "acc-p", status="waiting_operator",
                   deadline=LIVE_DEADLINE)
    r = client.get(f"/panel/auth/station/{sid}/status")
    assert r.status_code == 200
    payload = r.json()
    assert payload["id"] == sid and payload["status"] == "waiting_operator"
    assert payload["live"] is True
    assert payload["source_label"] == "人民法院案例库"
    assert payload["account_alias"] == "acc-p"
    # unknown station -> 404 (the poller just keeps its previous state)
    assert client.get("/panel/auth/station/999999/status").status_code == 404


def test_reclaim_route_tears_station_down(session, monkeypatch):
    _source("rmfyalk")
    station_ops.ensure_identity_with_egress(session, "rmfyalk", "acc-rc",
                                            now=NOW)
    fake = FakeStationClient()
    monkeypatch.setattr(appmod, "_station_client", lambda: fake)
    created = station_ops.create_station(session, "rmfyalk", "acc-rc",
                                         client=fake, now=NOW)
    sid = created["station_id"]

    r = client.post(f"/panel/auth/station/{sid}/reclaim",
                    headers={"hx-request": "true"})

    assert r.status_code == 200
    assert "reclaimed" in r.headers.get("hx-trigger", "")
    assert fake.created == []
    s = _db()
    try:
        assert s.get(CrawlLoginStation, sid).status == "reclaimed"
    finally:
        s.close()
    # a terminal row offers no reclaim, but does offer a one-click relaunch
    assert "/reclaim" not in r.text
    assert "重新拉起 relaunch" in r.text


def test_proxy_credentials_never_render_in_failure_reason(session):
    """The failure reason is escape-hatched from the row note — a note that
    quotes the proxy URL must not leak its credentials into the panel."""
    _source("rmfyalk", auth_profile="rmfyalk-login")
    iid = _ident("rmfyalk", "acc-leak", "login_required")
    sid = _station(iid, "rmfyalk", "acc-leak", status="failed",
                   note="Error: proxy http://fdproxy:Sup3rSecret@1.2.3.4:3128 "
                        "refused")
    r = client.get(f"/panel/auth/station/{sid}/view")
    assert "Sup3rSecret" not in r.text


# ── HTTP reverse proxy ──────────────────────────────────────────────────────
class _UpstreamHandler(BaseHTTPRequestHandler):
    seen: list[str] = []
    body = b"<html>novnc-page</html>"
    content_type = "text/html"

    def do_GET(self):  # noqa: N802 - http.server API
        _UpstreamHandler.seen.append(self.path)
        self.send_response(200)
        self.send_header("Content-Type", self.content_type)
        self.send_header("Content-Length", str(len(self.body)))
        self.end_headers()
        self.wfile.write(self.body)

    def log_message(self, *args):  # silence the test server
        pass


@pytest.fixture
def upstream_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _UpstreamHandler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    _UpstreamHandler.seen = []
    yield f"127.0.0.1:{port}"
    server.shutdown()


def test_vnc_reverse_proxy_forwards_path_and_query(session, monkeypatch,
                                                   upstream_server):
    monkeypatch.setattr(appmod, "_station_upstream",
                        lambda sid: upstream_server)
    r = client.get("/panel/auth/station/1/vnc/vnc.html"
                   "?autoconnect=1&path=websockify")
    assert r.status_code == 200
    assert "novnc-page" in r.text
    assert r.headers["content-type"].startswith("text/html")
    # the upstream saw the proxied path + query, un-rewritten
    assert _UpstreamHandler.seen == ["/vnc.html?autoconnect=1&path=websockify"]


def test_vnc_reverse_proxy_unreachable_is_friendly_502(session, monkeypatch):
    monkeypatch.setattr(appmod, "_station_upstream", lambda sid: "127.0.0.1:1")
    monkeypatch.setattr(appmod, "STATION_PROXY_ATTEMPTS", 1)   # no boot wait
    r = client.get("/panel/auth/station/1/vnc/vnc.html")
    assert r.status_code == 502          # friendly page, not a 500
    assert "登录站暂不可达" in r.text and "unreachable" in r.text


def test_vnc_reverse_proxy_unknown_station(session):
    # real resolver: no such station -> friendly 404, upstream never contacted
    r = client.get("/panel/auth/station/999/vnc/vnc.html")
    assert r.status_code == 404
    assert "不可用" in r.text


# ── websocket relay ─────────────────────────────────────────────────────────
@pytest.fixture
def echo_ws_port():
    import websockets

    holder: dict[str, int] = {}

    def run():
        async def handler(ws):
            async for message in ws:
                await ws.send(message)

        async def main():
            async with websockets.serve(handler, "127.0.0.1", 0) as srv:
                holder["port"] = srv.sockets[0].getsockname()[1]
                await asyncio.Event().wait()

        asyncio.run(main())

    threading.Thread(target=run, daemon=True).start()
    for _ in range(200):
        if "port" in holder:
            return holder["port"]
        time.sleep(0.05)
    raise RuntimeError("echo server did not start")


def test_websockify_relays_both_directions(session, monkeypatch, echo_ws_port):
    monkeypatch.setattr(appmod, "_station_upstream",
                        lambda sid: f"127.0.0.1:{echo_ws_port}")
    with client.websocket_connect("/panel/auth/station/1/websockify") as ws:
        ws.send_text("hello-rfb")
        assert ws.receive_text() == "hello-rfb"
        ws.send_bytes(b"\x01\x02\x03")
        assert ws.receive_bytes() == b"\x01\x02\x03"


def test_websockify_vnc_relative_path_relays_too(session, monkeypatch,
                                                 echo_ws_port):
    # noVNC's default path=websockify resolves relative to vnc.html -> the
    # nested route must relay exactly like the flat one
    monkeypatch.setattr(appmod, "_station_upstream",
                        lambda sid: f"127.0.0.1:{echo_ws_port}")
    with client.websocket_connect(
            "/panel/auth/station/1/vnc/websockify") as ws:
        ws.send_text("via-nested")
        assert ws.receive_text() == "via-nested"


def test_websockify_requires_panel_auth(session, monkeypatch):
    monkeypatch.setenv("PANEL_TOKEN", "sekret")
    # no credential -> closed 4401, no relay attempted (the close surfaces on
    # the first receive)
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect("/panel/auth/station/1/websockify") as ws:
            ws.receive_text()
    assert exc.value.code == 4401
    # the token as a query param authorizes (then 4404: station does not exist)
    with pytest.raises(WebSocketDisconnect) as exc2:
        with client.websocket_connect(
                "/panel/auth/station/1/websockify?token=sekret") as ws:
            ws.receive_text()
    assert exc2.value.code == 4404


# ── MCP auth_launch_login (task 3.4) ────────────────────────────────────────
def _unwrap(result):
    sc = getattr(result, "structured_content", None)
    if sc is not None:
        return sc.get("result", sc)
    if isinstance(result, tuple):
        return result[1]
    return getattr(result, "data", result)


def _call(name, args):
    from fd_open_data_mcp.server import mcp

    return _unwrap(asyncio.run(mcp.call_tool(name, args)))


def test_auth_launch_login_tool_registered():
    tools = asyncio.run(_tools())
    assert "auth_launch_login" in tools


def _tools():
    from fd_open_data_mcp.server import mcp

    async def run():
        return {t.name for t in await mcp.list_tools()}

    return run()


def test_auth_launch_login_launches_station_with_relative_url(session,
                                                              monkeypatch):
    _source("rmfyalk")
    pid = _proxy(ip="10.0.0.9")
    fake = FakeStationClient()
    monkeypatch.setattr(station_ops, "get_station_client", lambda: fake)

    out = _call("auth_launch_login",
                {"source": "rmfyalk", "account_alias": "acc-mcp"})

    assert out["status"] == "launched"
    # the egress the station dials through was assigned + returned
    assert out["egress_ref"] == f"proxy:{pid}"
    # vnc_url is RELATIVE and credential-free (the channel is panel-gated)
    assert out["vnc_url"].startswith("/panel/auth/station/")
    assert "vnc.html?autoconnect=1" in out["vnc_url"]
    assert "token" not in out["vnc_url"]
    assert len(fake.manifests("Job")) == 1
    assert len(fake.manifests("Service")) == 1

    # auth_status now carries the in-flight station count
    status = _call("auth_status", {})
    assert status["stations"] == {"active": 1}
    assert status["login_required"] == 1  # the identity sits in the queue


def test_auth_launch_login_resets_and_reuses_identity(session, monkeypatch):
    _source("rmfyalk")
    fake = FakeStationClient()
    monkeypatch.setattr(station_ops, "get_station_client", lambda: fake)
    _call("auth_launch_login", {"source": "rmfyalk", "account_alias": "acc-1"})
    out = _call("auth_launch_login",
                {"source": "rmfyalk", "account_alias": "acc-1"})
    assert out["status"] == "launched" and out["created"] is False
    assert len(fake.manifests("Job")) == 2  # one station per launch
    status = _call("auth_status", {})
    assert status["stations"]["active"] == 2
    assert status["total_identities"] == 1  # same identity, reset

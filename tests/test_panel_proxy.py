"""Panel proxy module tests (panel-ops-console, spec crawl-control-center).

Covers: /panel/proxy read page (masking!), polled egress-matrix partial,
management wiring to proxy-control (stubbed) with read-only fallback, the
inherited PANEL_TOKEN gate, and the fleet partial's egress column.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from fd_open_data_mcp.models import (
    Cluster, Proxy, SourceProxyHealth, SourceRateLimit,
)
from fd_open_data_mcp.panel.app import app

client = TestClient(app)

AUTH = "s7coink1:d1269o0"  # must NEVER appear unmasked in a response body


def _seed_proxy(session, **over) -> Proxy:
    row = Proxy(scheme="http", ip="122.246.5.153", port=20138, auth=AUTH,
                status="active", label="paid-static-122.246.5.153:20138",
                provider="paid-static")
    row.__dict__.update(over)
    session.add(row)
    session.commit()
    return row


# ─── 3.1 read page + masking ─────────────────────────────────────────────────
def test_proxy_page_renders_and_masks_credentials(session):
    _seed_proxy(session)
    session.add(SourceProxyHealth(source="eastmoney", proxy_id=1,
                                  state="open", fail_streak=3))
    session.add(SourceRateLimit(source="eastmoney", max_qps=0.5, max_concurrent=2))
    session.commit()
    r = client.get("/panel/proxy")
    assert r.status_code == 200
    assert "paid-static" in r.text and "eastmoney" in r.text
    # spec: the full credential value SHALL NOT appear in any panel response
    assert AUTH not in r.text
    assert "s7•••••" in r.text


def test_proxy_partial_matrix(session):
    p = _seed_proxy(session)
    session.add(SourceProxyHealth(source="eastmoney", proxy_id=p.id,
                                  state="half_open", fail_streak=0))
    session.commit()
    r = client.get("/panel/partials/proxy")
    assert r.status_code == 200
    assert "eastmoney" in r.text and "half_open" in r.text


def test_ban_rules_listed(session):
    from fd_open_data_mcp.models import BanRule
    session.add(BanRule(source="eastmoney", rule_type="status", pattern="403",
                        classification="ban"))
    session.commit()
    r = client.get("/panel/proxy")
    assert r.status_code == 200 and "403" in r.text


# ─── 3.2 management wiring ───────────────────────────────────────────────────
def test_readonly_fallback_when_control_unset(session, monkeypatch):
    monkeypatch.delenv("PROXY_CONTROL_URL", raising=False)
    _seed_proxy(session)
    r = client.get("/panel/proxy")
    assert r.status_code == 200 and "read-only" in r.text
    # management POST degrades to a redirect with the error, not a 500
    resp = client.post("/panel/proxy/import", data={"provider": "paid-static",
                                                    "text": "1.2.3.4|8080"},
                       follow_redirects=False)
    assert resp.status_code == 303 and "err=" in resp.headers["location"]


def test_import_and_status_via_stubbed_control(session, monkeypatch):
    calls: list[tuple] = []

    def _stub(method, path, payload=None):
        calls.append((method, path, payload))
        if path == "/management/proxies/import":
            return {"imported": 3, "skipped": 0, "updated": 0, "rows": []}
        return {"status": "ok", "proxy_status": "retired"}

    monkeypatch.setenv("PROXY_CONTROL_URL", "http://proxy-control:30090")
    monkeypatch.setattr("fd_open_data_mcp.panel.app._proxy_control", _stub)
    r = client.post("/panel/proxy/import",
                    data={"provider": "paid-static", "text": "1.2.3.4|8080|u|p"},
                    follow_redirects=True)
    assert r.status_code == 200 and "imported 3" in r.text
    p = _seed_proxy(session)
    r = client.post(f"/panel/proxy/{p.id}/status",
                    data={"status": "retired", "actor": "op"}, follow_redirects=True)
    assert r.status_code == 200 and "retired" in r.text
    assert calls[1] == ("POST", f"/management/proxies/{p.id}/status",
                        {"status": "retired", "actor": "op"})


def test_token_gate_covers_proxy_routes(session, monkeypatch):
    monkeypatch.setenv("PANEL_TOKEN", "sekret")
    from importlib import reload
    import fd_open_data_mcp.panel.app as appmod
    gated = TestClient(reload(appmod).app)
    assert gated.get("/panel/proxy").status_code == 401
    assert gated.get("/panel/partials/proxy").status_code == 401
    assert gated.get("/panel/proxy", params={"token": "sekret"}).status_code == 200
    monkeypatch.undo()
    reload(appmod)  # don't leak the gated app to later runtime imports


# ─── 3.3 fleet partial egress column ─────────────────────────────────────────
def test_fleet_partial_shows_egress_state(session):
    from fd_open_data_mcp.visibility import snapshot

    c = Cluster(name="c1", api_server="https://c1:6443", namespace="scraw",
                tags=[], capacity=2)
    session.add(c)
    session.commit()
    direct = Proxy(scheme="direct", ip="direct", status="active",
                   label="egress", cluster_id=c.id)
    session.add(direct)
    session.commit()
    session.add(SourceProxyHealth(source="eastmoney", proxy_id=direct.id,
                                  state="open", fail_streak=9))
    session.commit()
    fleet = snapshot.fleet_health(session)
    row = [f for f in fleet if f["name"] == "c1"][0]
    assert row["egress_known"] is True and row["egress_banned"] == ["eastmoney"]
    html = client.get("/panel/partials/fleet").text
    assert "1 banned" in html and "eastmoney" in html

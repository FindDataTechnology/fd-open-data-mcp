"""Auth identity-pool MCP tool tests (session-pool 4.2).

Follows the call pattern of test_platform_tools: tools are invoked on the
global FastMCP instance over the per-test sqlite fixture. Covers the read
surfaces (auth_status overview + per-source detail, auth_events stream) and
the one write — auth_request_login registers/resets an identity to
login_required and only writes rows (a note event lands, no login is
performed, no other column is clobbered).
"""
from __future__ import annotations

import asyncio
import datetime as dt

from fd_open_data_mcp.db import get_database
from fd_open_data_mcp.models import CrawlIdentity, CrawlIdentityEvent

NOW = dt.datetime.utcnow()
BASE = NOW - dt.timedelta(days=7)


def _unwrap(result):
    """fastmcp ToolResult — structured_content wraps the value as {'result': …}."""
    sc = getattr(result, "structured_content", None)
    if sc is not None:
        return sc.get("result", sc)
    if isinstance(result, tuple):
        return result[1]
    return getattr(result, "data", result)


def _call(name, args):
    from fd_open_data_mcp.server import mcp
    return _unwrap(asyncio.run(mcp.call_tool(name, args)))


def _tools():
    from fd_open_data_mcp.server import mcp
    return {t.name for t in asyncio.run(mcp.list_tools())}


# ── seed helpers ────────────────────────────────────────────────────────────
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


# ── registration ─────────────────────────────────────────────────────────────
def test_auth_tools_registered():
    tools = _tools()
    for name in ("auth_status", "auth_events", "auth_request_login"):
        assert name in tools


# ── auth_status ──────────────────────────────────────────────────────────────
def test_auth_status_pool_overview_per_source(session):
    _ident("rmfyalk", "acc-a", "active")
    _ident("rmfyalk", "acc-b", "login_required", failure_count=2)
    _ident("rmfyalk", "acc-c", "active", lease_owner="disp-1",
           lease_expires_at=NOW + dt.timedelta(hours=1))
    _ident("law", "acc-x", "banned", lease_owner="disp-2",
           lease_expires_at=NOW - dt.timedelta(minutes=5))  # TTL passed

    out = _call("auth_status", {})
    assert out["total_identities"] == 4
    assert out["login_required"] == 1 and out["active"] == 2
    assert out["leased"] == 1  # acc-c holds a live lease; acc-x's TTL passed
    by_src = {b["source"]: b for b in out["sources"]}
    assert by_src["rmfyalk"]["total"] == 3
    assert by_src["rmfyalk"]["statuses"] == {"active": 2, "login_required": 1}
    assert by_src["rmfyalk"]["leased"] == 1
    assert by_src["law"]["statuses"] == {"banned": 1}
    # no source filter -> no detail rows (overview shape)
    assert "identities" not in out


def test_auth_status_source_detail_rows(session):
    _ident("rmfyalk", "acc-a", "active", automation="auto",
           consecutive_zero_runs=3, last_login_at=BASE)
    _ident("other", "acc-z", "login_required")

    out = _call("auth_status", {"source": "rmfyalk"})
    assert out["source"] == "rmfyalk"
    assert out["total_identities"] == 1
    assert [r["account_alias"] for r in out["identities"]] == ["acc-a"]
    row = out["identities"][0]
    assert row["status"] == "active" and row["automation"] == "auto"
    assert row["consecutive_zero_runs"] == 3
    assert row["last_login_at"] == BASE.isoformat()
    assert row["leased"] is False

    # a source with no pool is zero totals + empty rows, not an error
    empty = _call("auth_status", {"source": "ghost"})
    assert empty["total_identities"] == 0 and empty["identities"] == []
    assert empty["sources"] == []


# ── auth_events ──────────────────────────────────────────────────────────────
def test_auth_events_stream_newest_first_filtered_limited(session):
    a = _ident("rmfyalk", "acc-a")
    b = _ident("law", "acc-b")
    _event(a, "login", detail="session captured", created=BASE)
    _event(a, "lease_acquired", lease_token="tok-1",
           created=BASE + dt.timedelta(minutes=10))
    _event(b, "auth_failed", detail="401 wall", created=BASE + dt.timedelta(minutes=5))

    events = _call("auth_events", {})
    assert [(e["kind"], e["account_alias"]) for e in events] == [
        ("lease_acquired", "acc-a"),   # newest first
        ("auth_failed", "acc-b"),
        ("login", "acc-a")]
    head = events[0]
    assert head["source"] == "rmfyalk" and head["lease_token"] == "tok-1"
    assert "session captured" in events[2]["detail"]

    only_law = _call("auth_events", {"source": "law"})
    assert [e["kind"] for e in only_law] == ["auth_failed"]

    for i in range(5):
        _event(a, "note", detail=f"n{i}", created=BASE + dt.timedelta(hours=i))
    limited = _call("auth_events", {"limit": 2})
    assert len(limited) == 2
    assert [e["detail"] for e in limited] == ["n4", "n3"]


# ── auth_request_login ───────────────────────────────────────────────────────
def test_auth_request_login_registers_new_identity(session):
    out = _call("auth_request_login",
                {"source": "rmfyalk", "account_alias": "acc-new",
                 "automation": "auto"})
    assert out["status"] == "queued" and out["created"] is True
    assert out["previous_status"] is None
    assert "login site" in out["note"]
    s = get_database().get_session()
    try:
        row = s.get(CrawlIdentity, out["identity_id"])
        assert row.source == "rmfyalk" and row.account_alias == "acc-new"
        assert row.status == "login_required"   # lands straight in the queue
        assert row.automation == "auto"
        events = s.query(CrawlIdentityEvent).filter_by(identity_id=row.id).all()
        assert len(events) == 1 and events[0].kind == "note"
        assert "mcp" in events[0].detail and "auto" in events[0].detail
    finally:
        s.close()
    # the new identity shows up in the read surfaces
    assert _call("auth_status", {})["login_required"] == 1


def test_auth_request_login_resets_existing_and_clears_lease(session):
    iid = _ident("rmfyalk", "acc-live", "active",
                 lease_owner="disp-1", lease_token="tok-9",
                 lease_expires_at=NOW + dt.timedelta(hours=1),
                 failure_count=7, consecutive_zero_runs=2)
    _event(iid, "login", detail="first login", created=BASE)

    out = _call("auth_request_login",
                {"source": "rmfyalk", "account_alias": "acc-live"})
    assert out["status"] == "queued" and out["created"] is False
    assert out["previous_status"] == "active"
    s = get_database().get_session()
    try:
        row = s.get(CrawlIdentity, iid)
        s.refresh(row)
        assert row.status == "login_required"   # reset into the queue
        assert row.lease_owner is None and row.lease_token is None
        assert row.lease_expires_at is None     # never appears leased
        # ON CONFLICT DO NOTHING: diagnostics survive the reset
        assert row.failure_count == 7 and row.consecutive_zero_runs == 2
        assert row.automation == "assisted"     # original automation kept
        events = s.query(CrawlIdentityEvent).filter_by(identity_id=iid) \
            .order_by(CrawlIdentityEvent.id).all()
        assert [e.kind for e in events] == ["login", "note"]  # audit grows
        assert "previous status: active" in events[1].detail
    finally:
        s.close()

    # idempotent re-request: still queued, another note appended
    out2 = _call("auth_request_login",
                 {"source": "rmfyalk", "account_alias": "acc-live"})
    assert out2["status"] == "queued" and out2["created"] is False
    assert out2["previous_status"] == "login_required"


def test_auth_request_login_invalid_automation_writes_nothing(session):
    out = _call("auth_request_login",
                {"source": "rmfyalk", "account_alias": "acc-bad",
                 "automation": "headful"})
    assert out["status"] == "invalid"
    assert "auto" in out["reason"] and "assisted" in out["reason"]
    s = get_database().get_session()
    try:
        assert s.query(CrawlIdentity).count() == 0
        assert s.query(CrawlIdentityEvent).count() == 0
    finally:
        s.close()


def test_mcp_login_request_is_the_shared_operation(session):
    """Same contract as the platform tools: one shared op, the entrance only
    chooses requested_by — the audit trail records who asked."""
    import fd_open_data_mcp.auth_tools as at

    s = get_database().get_session()
    try:
        out = at.request_identity_login(s, "rmfyalk", "acc-both",
                                        requested_by="panel")
        assert out["status"] == "queued"
        ev = (s.query(CrawlIdentityEvent)
              .filter_by(identity_id=out["identity_id"]).one())
        assert "by panel" in ev.detail
    finally:
        s.close()
    mcp_out = _call("auth_request_login",
                    {"source": "rmfyalk", "account_alias": "acc-both"})
    assert mcp_out["status"] == "queued" and mcp_out["created"] is False
    s = get_database().get_session()
    try:
        details = [e.detail for e in s.query(CrawlIdentityEvent)
                   .order_by(CrawlIdentityEvent.id).all()]
        assert any("by mcp" in d for d in details)
    finally:
        s.close()

"""Platform control MCP tool tests (crawl-platform task 4.3).

Follows the call pattern of the other tool tests (test_data_stats_summary):
tools are invoked on the global FastMCP instance over the per-test sqlite
fixture. Covers the read surfaces (sources with stall hints, run history with
trigger lineage) and the write CAS semantics — and that the MCP trigger is the
same operation the panel button performs (shared op, identical guardrails).
"""
from __future__ import annotations

import asyncio
import datetime as dt

from fd_open_data_mcp.db import get_database
from fd_open_data_mcp.models import CrawlRun, CrawlSite, CrawlSource, PendingRun

NOW = dt.datetime.utcnow()


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


# ── seed helpers (mirror test_panel_platform's) ─────────────────────────────
def _seed(source, site="tencent", schedule="0 6 * * *", enabled=True,
          last_commit="c0ffee1"):
    s = get_database().get_session()
    try:
        if s.get(CrawlSite, site) is None:
            s.add(CrawlSite(id=site, enabled=True))
        if s.get(CrawlSource, source) is None:
            s.add(CrawlSource(source=source, site=site, schedule=schedule,
                              enabled=enabled, last_commit=last_commit))
        s.commit()
    finally:
        s.close()


def _seed_run(source, status, *, started=None, finished=None, rows=0,
              pending_id=None) -> int:
    s = get_database().get_session()
    try:
        r = CrawlRun(source=source, status=status,
                     started_at=started or NOW, finished_at=finished,
                     rows_written=rows, pending_run_id=pending_id)
        s.add(r)
        s.commit()
        return r.id
    finally:
        s.close()


# ── registration ─────────────────────────────────────────────────────────────
def test_platform_tools_registered():
    tools = _tools()
    for name in ("platform_sources", "platform_runs", "platform_trigger",
                 "platform_cancel_run", "platform_cancel_pending"):
        assert name in tools


# ── reads ────────────────────────────────────────────────────────────────────
def test_platform_sources_inventory_with_health(session):
    _seed("ok-src")                        # lit, ran recently
    _seed_run("ok-src", "success", started=NOW - dt.timedelta(hours=2),
              finished=NOW - dt.timedelta(hours=1), rows=64)
    _seed("old-src")                       # lit, ran 9 days ago -> stalled
    _seed_run("old-src", "success", started=NOW - dt.timedelta(days=9),
              finished=NOW - dt.timedelta(days=9))
    _seed("dark-src", schedule=None)       # unlit
    _seed("off-src", enabled=False)        # paused

    payload = _call("platform_sources", {})
    assert payload["total"] == 4
    assert payload["lit"] == 3
    assert payload["stalled"] == 1  # old-src only
    by_src = {r["source"]: r for r in payload["sources"]}
    assert by_src["ok-src"]["last_run_status"] == "success"
    assert by_src["ok-src"]["last_run_rows"] == 64
    assert by_src["ok-src"]["stalled"] is False
    assert by_src["old-src"]["stalled"] is True
    assert by_src["dark-src"]["schedule"] is None
    assert by_src["off-src"]["enabled"] is False

    # site filter narrows the listing
    only = _call("platform_sources", {"site": "nobody"})
    assert only["total"] == 0 and only["sources"] == []


def test_platform_runs_history_with_lineage(session):
    _seed("m-src")
    s = get_database().get_session()
    try:
        p = PendingRun(source="m-src", site="tencent", params={"limit": 5},
                       requested_by="mcp", status="claimed")
        s.add(p)
        s.commit()
        pid = p.id
    finally:
        s.close()
    _seed_run("m-src", "running", started=NOW - dt.timedelta(minutes=4),
              pending_id=pid)
    other = _seed_run("other-src", "success", finished=NOW, rows=3)
    _seed("other-src")

    runs = _call("platform_runs", {})
    assert {r["source"] for r in runs} == {"m-src", "other-src"}
    mine = [r for r in runs if r["source"] == "m-src"][0]
    assert mine["pending_run_id"] == pid
    assert mine["pending_requested_by"] == "mcp"   # trigger lineage attached
    assert mine["cancel_requested"] is None

    only = _call("platform_runs", {"source": "other-src"})
    assert [r["id"] for r in only] == [other]
    limited = _call("platform_runs", {"limit": 1})
    assert len(limited) == 1


# ── trigger ──────────────────────────────────────────────────────────────────
def test_platform_trigger_queues_pending_row(session):
    _seed("trig-src")
    out = _call("platform_trigger", {"source": "trig-src", "limit": 2})
    assert out["status"] == "triggered" and isinstance(out["pending_id"], int)
    assert out["site"] == "tencent"
    s = get_database().get_session()
    try:
        p = s.get(PendingRun, out["pending_id"])
        assert p.source == "trig-src" and p.status == "pending"
        assert p.requested_by == "mcp"
        assert p.params == {"limit": 2}      # param override recorded
        assert p.site == "tencent"
    finally:
        s.close()
    # no limit -> params stays an empty dict, not None
    out2 = _call("platform_trigger", {"source": "trig-src"})
    assert out2["status"] == "triggered"
    s = get_database().get_session()
    try:
        p2 = s.get(PendingRun, out2["pending_id"])
        assert p2.params == {}
    finally:
        s.close()


def test_platform_trigger_guardrails_match_panel(session):
    # unregistered source: clear refusal text, nothing written
    out = _call("platform_trigger", {"source": "ghost"})
    assert out["status"] == "not_found"
    assert "not registered" in out["reason"]
    # disabled source
    _seed("trig-off", enabled=False)
    out = _call("platform_trigger", {"source": "trig-off"})
    assert out["status"] == "refused" and "disabled" in out["reason"]
    # federated member without a content-repo manifest: refused with a pointer
    # (a queued row would only produce a bogus EXIT_SOURCE_MISSING failed run)
    _seed("trig-fed", schedule=None, last_commit=None)
    out = _call("platform_trigger", {"source": "trig-fed"})
    assert out["status"] == "refused" and "federated member" in out["reason"]
    # single-flight: an open run blocks the trigger
    _seed("trig-open")
    _seed_run("trig-open", "running", started=NOW - dt.timedelta(minutes=1))
    out = _call("platform_trigger", {"source": "trig-open"})
    assert out["status"] == "refused" and "single-flight" in out["reason"]
    s = get_database().get_session()
    try:
        assert s.query(PendingRun).count() == 0
    finally:
        s.close()


def test_mcp_trigger_is_the_panel_operation(session):
    """Spec scenario: an MCP trigger produces exactly the row the panel button
    would — same op, same defaults, only requested_by differs."""
    import fd_open_data_mcp.platform_tools as pt

    _seed("equiv-src")
    s = get_database().get_session()
    try:
        panel_out = pt.trigger_platform_run(s, "equiv-src", requested_by="panel")
        mcp_out = pt.trigger_platform_run(s, "equiv-src", requested_by="mcp")
        assert panel_out["status"] == mcp_out["status"] == "triggered"
        assert panel_out["site"] == mcp_out["site"] == "tencent"
        rows = s.query(PendingRun).filter_by(source="equiv-src").all()
        assert [r.requested_by for r in rows] == ["panel", "mcp"]
        assert all(r.status == "pending" and r.site == "tencent"
                   for r in rows)
    finally:
        s.close()


# ── cancels ──────────────────────────────────────────────────────────────────
def test_platform_cancel_run_cas(session):
    _seed("cr-src")
    rid = _seed_run("cr-src", "running", started=NOW - dt.timedelta(minutes=2))
    out = _call("platform_cancel_run", {"run_id": rid})
    assert out["status"] == "cancel_requested"
    s = get_database().get_session()
    try:
        run = s.get(CrawlRun, rid)
        s.refresh(run)
        assert run.cancel_requested is not None
        assert run.status == "running"  # flag only; the runner closes the row
    finally:
        s.close()

    done = _seed_run("cr-src", "success", finished=NOW)
    out = _call("platform_cancel_run", {"run_id": done})
    assert out["status"] == "already_finished"
    assert out["current_status"] == "success"
    assert "already finished" in out["reason"]

    assert _call("platform_cancel_run", {"run_id": 424242}) == {
        "status": "not_found"}


def test_platform_cancel_pending_cas(session):
    _seed("cp-src")
    s = get_database().get_session()
    try:
        pend = PendingRun(source="cp-src", site="tencent", requested_by="mcp")
        claimed = PendingRun(source="cp-src", site="tencent",
                             requested_by="mcp", status="claimed")
        done = PendingRun(source="cp-src", site="tencent",
                          requested_by="mcp", status="done")
        s.add_all([pend, claimed, done])
        s.commit()
        pend_id, claimed_id, done_id = pend.id, claimed.id, done.id
    finally:
        s.close()

    out = _call("platform_cancel_pending", {"pending_id": pend_id})
    assert out["status"] == "cancelled"
    out2 = _call("platform_cancel_pending", {"pending_id": claimed_id})
    assert out2["status"] == "cancelled"  # claimed rows still cancellable

    s = get_database().get_session()
    try:
        for i in (pend_id, claimed_id):
            row = s.get(PendingRun, i)
            s.refresh(row)
            assert row.status == "cancelled" and row.finished_at is not None
    finally:
        s.close()

    out3 = _call("platform_cancel_pending", {"pending_id": done_id})
    assert out3["status"] == "already_finished"
    assert out3["current_status"] == "done"
    assert _call("platform_cancel_pending", {"pending_id": 98765}) == {
        "status": "not_found"}
    # already-cancelled re-request is refused, not a second write
    out4 = _call("platform_cancel_pending", {"pending_id": pend_id})
    assert out4["status"] == "already_finished"

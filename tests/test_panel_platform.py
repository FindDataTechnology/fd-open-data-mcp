"""Panel platform-view + control tests (crawl-platform tasks 4.1/4.2).

Covers: the platform source inventory (filters, stall hints, trigger buttons),
source detail with pending lineage, side-by-side platform runs on /panel/runs,
the cockpit health partial (counts + degradation), and the write operations —
trigger (guardrails: unregistered / disabled / single-flight / DB fallback),
cancel-run (CAS flag) and cancel-pending (CAS status). All data is seeded
directly through the models on the per-test sqlite fixture.
"""
from __future__ import annotations

import datetime as dt
import json

from fastapi.testclient import TestClient

from fd_open_data_mcp.db import get_database
from fd_open_data_mcp.models import (
    CrawlPolicy, CrawlRun, CrawlSite, CrawlSource, PendingRun, PolicyRun,
)
from fd_open_data_mcp.panel.app import app

client = TestClient(app)
HX = {"HX-Request": "true"}


# ── seed helpers ────────────────────────────────────────────────────────────
def _site(sid="tencent") -> str:
    s = get_database().get_session()
    try:
        if s.get(CrawlSite, sid) is None:
            s.add(CrawlSite(id=sid, description="main site", kind="k8s",
                            enabled=True))
            s.commit()
        return sid
    finally:
        s.close()


def _source(name, site="tencent", schedule="0 6 * * *", enabled=True) -> str:
    _site(site)
    s = get_database().get_session()
    try:
        if s.get(CrawlSource, name) is None:
            s.add(CrawlSource(source=name, site=site, schedule=schedule,
                              enabled=enabled, last_commit="abc123"))
            s.commit()
        return name
    finally:
        s.close()


def _run(source, status, *, started=None, finished=None, rows=None,
         pending_id=None) -> int:
    s = get_database().get_session()
    try:
        r = CrawlRun(source=source, status=status,
                     started_at=started or dt.datetime.utcnow(),
                     finished_at=finished,
                     rows_written=rows, pending_run_id=pending_id)
        s.add(r)
        s.commit()
        return r.id
    finally:
        s.close()


def _pending(source, site="tencent", status="pending",
             requested_by="panel") -> int:
    s = get_database().get_session()
    try:
        p = PendingRun(source=source, site=site, params={},
                       requested_by=requested_by, status=status)
        s.add(p)
        s.commit()
        return p.id
    finally:
        s.close()


NOW = dt.datetime.utcnow()


def _toast_msg(resp) -> str:
    """HX-Trigger toasts carry non-ASCII escaped as \\uXXXX — parse, don't grep."""
    return json.loads(resp.headers["HX-Trigger"])["toast"]["message"]


# ── 4.1 source inventory ─────────────────────────────────────────────────────
def test_sources_list_renders_inventory_with_filters(session):
    _source("lit-fresh", schedule="0 6 * * *")
    _run("lit-fresh", "success", started=NOW - dt.timedelta(hours=2),
         finished=NOW - dt.timedelta(hours=1), rows=120)
    _source("lit-stale", schedule="0 6 * * *")
    _run("lit-stale", "success", started=NOW - dt.timedelta(days=9),
         finished=NOW - dt.timedelta(days=9), rows=5)
    _source("unlit-one", schedule=None)
    _source("disabled-one", schedule="0 6 * * *", enabled=False)

    page = client.get("/panel/sources").text
    assert "lit-fresh" in page and "unlit-one" in page
    assert "未点亮 not lit" in page
    assert "120" in page  # latest-run rows_written surfaced
    assert "立即触发 trigger" in page
    # stall hint on the stale lit source only (the chip label also says 停滞,
    # the row marker carries the ⚠ prefix)
    assert page.count("⚠ 停滞 stalled") == 1

    # health chips filter server-side; hx swaps only the results region
    stalled = client.get("/panel/sources", params={"health": "stalled"},
                         headers=HX)
    assert stalled.status_code == 200 and "<html" not in stalled.text
    assert "lit-stale" in stalled.text and "lit-fresh" not in stalled.text
    unlit = client.get("/panel/sources", params={"health": "unlit"}).text
    assert "unlit-one" in unlit and "lit-fresh" not in unlit
    # site filter narrows to one member site
    only = client.get("/panel/sources", params={"site": "tencent"}).text
    assert "lit-fresh" in only


def test_stalled_hint_requires_lit_and_enabled(session):
    from fd_open_data_mcp.visibility import snapshot

    _site("ghost-site")
    _source("s-old", site="ghost-site", schedule="0 6 * * *")
    _run("s-old", "success", started=NOW - dt.timedelta(days=8))
    _source("s-fresh", schedule="0 6 * * *")
    _run("s-fresh", "success", started=NOW - dt.timedelta(days=1))
    _source("s-never", schedule="0 6 * * *")            # lit, never ran
    _source("s-unlit", schedule=None)                   # unlit, never ran
    _source("s-off", schedule="0 6 * * *", enabled=False)  # paused lit

    s = get_database().get_session()
    try:
        rows = {r["source"]: r for r in snapshot.platform_sources(s)}
    finally:
        s.close()
    assert rows["s-old"]["stalled"] is True
    assert rows["s-fresh"]["stalled"] is False
    assert rows["s-never"]["stalled"] is True     # registered, no run at all
    assert rows["s-unlit"]["stalled"] is False    # nothing owed
    assert rows["s-off"]["stalled"] is False      # paused, nothing owed


def test_source_detail_metadata_runs_pending_and_404(session):
    _source("detail-src", schedule="*/5 * * * *")
    pid = _pending("detail-src", requested_by="panel")
    rid = _run("detail-src", "running", started=NOW - dt.timedelta(minutes=10),
               pending_id=pid)

    r = client.get("/panel/sources/detail-src")
    assert r.status_code == 200
    for marker in ("*/5 * * * *", "abc123", "tencent"):
        assert marker in r.text
    assert f"pending #{pid}" in r.text and "panel" in r.text  # trigger lineage
    assert f"/panel/runs/platform/{rid}/cancel" in r.text     # cancel affordance
    assert f"/panel/pending/{pid}/cancel" in r.text
    # lit + never finished within 7 days -> no stall banner (run is recent)
    assert "停滞提示" not in r.text
    assert client.get("/panel/sources/no-such-source").status_code == 404


def test_runs_page_shows_platform_runs_side_by_side(session):
    # policy run via the existing tables (untouched columns stay first)
    s = get_database().get_session()
    try:
        pol = CrawlPolicy(name="mix", concept_ids=[1], entity_type="stock",
                          date_policy={"mode": "trailing", "days": 1},
                          cron_expr="0 * * * *")
        s.add(pol)
        s.commit()
        s.add(PolicyRun(policy_id=pol.id, status="running"))
        s.commit()
        pid = pol.id
    finally:
        s.close()
    _source("mix-src")
    _run("mix-src", "success", finished=NOW, rows=7)

    page = client.get("/panel/runs").text
    assert "平台运行" in page and "Platform runs" in page  # second table present
    assert "mix-src" in page and "7" in page                # platform row rendered
    assert f"/panel/policies/{pid}" in page                 # policy columns unchanged


def test_platform_partial_counts_and_degrades(session):
    _source("h-fresh", schedule="0 6 * * *")
    _run("h-fresh", "success", started=NOW - dt.timedelta(hours=3),
         finished=NOW - dt.timedelta(hours=2), rows=10)
    _run("h-fresh", "failed", started=NOW - dt.timedelta(hours=5),
         finished=NOW - dt.timedelta(hours=4))
    _source("h-old", schedule="0 6 * * *")
    _run("h-old", "success", started=NOW - dt.timedelta(days=9),
         finished=NOW - dt.timedelta(days=9))
    _source("h-unlit", schedule=None)

    r = client.get("/panel/partials/platform")
    assert r.status_code == 200 and "<html" not in r.text
    for marker in (">3<", ">2<", ">1<", ">1<", ">1<"):  # total/lit/stalled/ok/failed
        assert marker in r.text
    assert "/panel/sources?health=stalled" in r.text  # stall drills down

    # home polls the partial; a failing section degrades like every other
    assert "/panel/partials/platform" in client.get("/panel").text
    from unittest.mock import patch
    from fd_open_data_mcp.visibility import snapshot
    with patch.object(snapshot, "platform_health",
                      side_effect=RuntimeError("db gone")):
        down = client.get("/panel/partials/platform")
        assert down.status_code == 200
        assert "section unavailable" in down.text


def test_platform_routes_behind_token_gate(session, monkeypatch):
    monkeypatch.setenv("PANEL_TOKEN", "sekret")
    from importlib import reload
    import fd_open_data_mcp.panel.app as appmod
    gated = TestClient(reload(appmod).app)
    assert gated.get("/panel/sources").status_code == 401
    assert gated.get("/panel/sources/x").status_code == 401
    assert gated.get("/panel/partials/platform").status_code == 401
    assert gated.post("/panel/sources/x/trigger").status_code == 401
    assert gated.post("/panel/runs/platform/1/cancel").status_code == 401
    assert gated.post("/panel/pending/1/cancel").status_code == 401
    ok = {"X-Panel-Token": "sekret"}
    assert gated.get("/panel/sources", headers=ok).status_code == 200
    assert gated.get("/panel/partials/platform", headers=ok).status_code == 200
    monkeypatch.undo()
    reload(appmod)


# ── 4.2 trigger ──────────────────────────────────────────────────────────────
def test_trigger_inserts_pending_row_and_swaps_row(session):
    _source("trig-ok")
    r = client.post("/panel/sources/trig-ok/trigger", headers=HX)
    assert r.status_code == 200
    assert "<html" not in r.text and "trig-ok" in r.text  # row re-rendered
    assert "queued" in r.headers.get("HX-Trigger", "")
    s = get_database().get_session()
    try:
        p = s.query(PendingRun).filter_by(source="trig-ok").one()
        assert p.status == "pending" and p.site == "tencent"
        assert p.requested_by == "panel"
    finally:
        s.close()
    # non-htmx posts redirect to the source detail
    r2 = client.post("/panel/sources/trig-ok/trigger", follow_redirects=False)
    assert r2.status_code == 303
    assert r2.headers["location"] == "/panel/sources/trig-ok"


def test_trigger_refusals_carry_clear_text(session):
    # unregistered source: refused both ways, nothing written
    r = client.post("/panel/sources/ghost/trigger")
    assert r.status_code == 404
    assert "not registered" in r.text
    hx = client.post("/panel/sources/ghost/trigger", headers=HX)
    assert hx.status_code == 200 and '"err"' in hx.headers["HX-Trigger"]
    assert "not registered" in hx.headers["HX-Trigger"]
    # disabled source
    _source("trig-off", enabled=False)
    r = client.post("/panel/sources/trig-off/trigger")
    assert r.status_code == 409 and "disabled" in r.text
    # single-flight: an open crawl_run blocks the trigger
    _source("trig-open")
    _run("trig-open", "running", started=NOW - dt.timedelta(minutes=5))
    r = client.post("/panel/sources/trig-open/trigger")
    assert r.status_code == 409 and "single-flight" in r.text
    hx = client.post("/panel/sources/trig-open/trigger", headers=HX)
    assert "single-flight" in hx.headers["HX-Trigger"]
    s = get_database().get_session()
    try:
        assert s.query(PendingRun).count() == 0  # no rows from refusals
    finally:
        s.close()


def test_trigger_integrity_error_becomes_friendly_text(session):
    # the DB-level site guard: a pending row for an unregistered site is
    # rejected by the FK (the same guard the insert fallback relies on)
    from sqlalchemy.exc import IntegrityError

    s = get_database().get_session()
    try:
        s.add(PendingRun(source="x", site="ghost-site", requested_by="t"))
        s.commit()
        assert False, "FK did not reject the ghost site"
    except IntegrityError:
        s.rollback()
    finally:
        s.close()

    # and the shared op converts an insert failure into operator text: the
    # wrapper delegates everything to the real session but fails on commit —
    # exactly where a DB-level rejection (bad FK) surfaces
    import fd_open_data_mcp.platform_tools as pt

    class _CommitBoom:
        def __init__(self, inner):
            self._inner = inner

        def __getattr__(self, name):
            return getattr(self._inner, name)

        def commit(self):
            raise IntegrityError("insert", {}, Exception("fk violation"))

    _source("trig-fk")
    s = get_database().get_session()
    try:
        out = pt.trigger_platform_run(_CommitBoom(s), "trig-fk",
                                      requested_by="panel")
    finally:
        s.close()
    assert out["status"] == "error"
    assert "crawl_sites" in out["reason"]

    # the panel surfaces that text as an error toast / 400, never a 500
    import fd_open_data_mcp.panel.app as appmod
    appmod_trigger = appmod.trigger_platform_run
    appmod.trigger_platform_run = (
        lambda s, src, requested_by: {"status": "error",
                                      "reason": "site 'ghost' not in crawl_sites"})
    try:
        hx = client.post("/panel/sources/trig-fk/trigger", headers=HX)
        assert hx.status_code == 200
        assert '"err"' in hx.headers["HX-Trigger"]
        assert "crawl_sites" in hx.headers["HX-Trigger"]
        plain = client.post("/panel/sources/trig-fk/trigger")
        assert plain.status_code == 400 and "crawl_sites" in plain.text
    finally:
        appmod.trigger_platform_run = appmod_trigger


# ── 4.2 cancel run / cancel pending ──────────────────────────────────────────
def test_cancel_platform_run_cas_semantics(session):
    _source("cr-src")
    rid = _run("cr-src", "running", started=NOW - dt.timedelta(minutes=3))

    r = client.post(f"/panel/runs/platform/{rid}/cancel", headers=HX)
    assert r.status_code == 200
    assert "cancel requested" in r.headers["HX-Trigger"]
    assert "取消请求中 cancel" in r.text  # row re-rendered with the flag
    s = get_database().get_session()
    try:
        run = s.get(CrawlRun, rid)
        s.refresh(run)
        assert run.cancel_requested is not None
        assert run.status == "running"  # flag only — the runner closes the row
    finally:
        s.close()

    # non-htmx posts redirect; re-request on a still-running row is fine
    r2 = client.post(f"/panel/runs/platform/{rid}/cancel",
                     follow_redirects=False)
    assert r2.status_code == 303

    # terminal row loses the CAS: 409 + text, row untouched
    done = _run("cr-src", "success", finished=NOW)
    r3 = client.post(f"/panel/runs/platform/{done}/cancel")
    assert r3.status_code == 409 and "already finished" in r3.text
    hx = client.post(f"/panel/runs/platform/{done}/cancel", headers=HX)
    assert '"err"' in hx.headers["HX-Trigger"]
    assert "already finished" in hx.headers["HX-Trigger"]
    assert client.post("/panel/runs/platform/99999/cancel").status_code == 404


def test_cancel_pending_cas_semantics(session):
    _source("cp-src")
    pend = _pending("cp-src", status="pending")
    claimed = _pending("cp-src", status="claimed")
    done = _pending("cp-src", status="done")

    r = client.post(f"/panel/pending/{pend}/cancel", headers=HX)
    assert r.status_code == 200
    assert "已取消 cancelled" in _toast_msg(r)
    assert "cancelled" in r.text  # re-rendered row shows the terminal badge
    s = get_database().get_session()
    try:
        row = s.get(PendingRun, pend)
        s.refresh(row)
        assert row.status == "cancelled" and row.finished_at is not None
    finally:
        s.close()

    # claimed rows are still cancellable; done rows are not
    assert client.post(f"/panel/pending/{claimed}/cancel",
                       follow_redirects=False).status_code == 303
    r = client.post(f"/panel/pending/{done}/cancel")
    assert r.status_code == 409 and "already done" in r.text
    hx = client.post(f"/panel/pending/{done}/cancel", headers=HX)
    assert "already done" in hx.headers["HX-Trigger"]
    assert client.post("/panel/pending/99999/cancel").status_code == 404
    # already-cancelled re-request is a 409 too (CAS on pending/claimed only)
    assert client.post(f"/panel/pending/{pend}/cancel").status_code == 409

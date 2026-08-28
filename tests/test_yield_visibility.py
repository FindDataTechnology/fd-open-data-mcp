"""Watcher + snapshot yield-signal tests (fix-silent-zero-yield-crawls Phases 3-4).

Proves: zero_yield alerts WITH plan_cells; no_op never alerts; a windowed
fetch count is zero when all rows fall outside the window (R6 regression);
recent runs surface recorded yield; redundant streaks and fleet yield are
computed; a frozen recurring policy is refused while trigger-now still may
run it as a backfill.
"""
from __future__ import annotations

import datetime as dt
from unittest.mock import MagicMock, patch

import pytest

from fd_open_data_mcp.models import CrawlPolicy, FetchLog, PolicyRun
from fd_open_data_mcp.visibility import scan, snapshot


def _mk_policy(session, name="p1", **kw) -> CrawlPolicy:
    defaults = dict(
        name=name, enabled=True, concept_ids=[1], entity_type="fund",
        cron_expr="0 6 * * *", timezone="UTC",
        date_policy={"mode": "since_last"}, frequency="daily", mode="per_date",
    )
    defaults.update(kw)
    p = CrawlPolicy(**defaults)
    session.add(p)
    session.commit()
    session.refresh(p)
    return p


def _mk_run(session, policy, status, *, finished=None, detail=None,
            plan_cells=None, rows_attempted=None, rows_new=None) -> PolicyRun:
    now = dt.datetime.now(dt.timezone.utc)
    r = PolicyRun(
        policy_id=policy.id, status=status, started_at=now, finished_at=finished,
        detail=detail, plan_cells=plan_cells, rows_attempted=rows_attempted,
        rows_new=rows_new,
    )
    session.add(r)
    session.commit()
    session.refresh(r)
    return r


@pytest.fixture
def no_redis(monkeypatch):
    monkeypatch.delenv("REDIS_URL", raising=False)
    scan.state._REDIS = None
    yield


@pytest.fixture
def stub_probe(monkeypatch):
    monkeypatch.setattr(snapshot, "_probe_cluster", lambda c: True)


@pytest.fixture
def quiet_notifier(monkeypatch):
    m = MagicMock()
    monkeypatch.setattr(scan, "get_notifier", lambda: m)
    yield m


# --- 3.1 / 3.2: zero_yield alerts with plan_cells; no_op does not -------------

def test_zero_yield_alerts_with_plan_cells(session, no_redis, stub_probe,
                                           quiet_notifier, monkeypatch):
    monkeypatch.delenv("SCRAW_STALE_MINUTES", raising=False)
    pol = _mk_policy(session, "zy")
    _mk_run(session, pol, "zero_yield", finished=dt.datetime.now(dt.timezone.utc),
            plan_cells=630)
    summary = scan.scan_once(session)
    assert summary["zero_yield"], "zero_yield run must alert"
    rec = summary["zero_yield"][0]
    assert rec["plan_cells"] == 630
    assert quiet_notifier.send.called


def test_no_op_does_not_alert(session, no_redis, stub_probe, quiet_notifier):
    pol = _mk_policy(session, "noop")
    _mk_run(session, pol, "no_op", finished=dt.datetime.now(dt.timezone.utc),
            plan_cells=0)
    summary = scan.scan_once(session)
    assert summary["failed"] == [] and summary["zero_yield"] == []
    assert not quiet_notifier.send.called


# --- 3.5 regression: a windowed count honours its window ----------------------

def test_windowed_fetch_count_zero_when_rows_outside_window(session):
    old = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=48)
    session.add(FetchLog(source="akshare", status="error",
                         real_source="eastmoney", timestamp=old))
    session.commit()
    rows = snapshot.per_source_outcome(session, hours=24)
    assert rows == [], "lifetime rows must not leak into a 24h window"


def test_summary_labels_carry_the_requested_window(session, monkeypatch, stub_probe):
    monkeypatch.setenv("SCRAW_STALE_MINUTES", "90")
    snap = snapshot.build_snapshot(session, hours=168)
    assert "fetches_ok_168h" in snap["summary"]
    assert "fetches_err_168h" in snap["summary"]
    assert "fetches_ok_24h" not in snap["summary"]


# --- 3.6: recent runs surface recorded yield -----------------------------------

def test_recent_runs_include_yield(session, stub_probe):
    pol = _mk_policy(session, "y")
    _mk_run(session, pol, "success", finished=dt.datetime.now(dt.timezone.utc),
            plan_cells=100, rows_attempted=5166, rows_new=400)
    runs = snapshot.recent_runs(session)
    assert runs[0]["plan_cells"] == 100
    assert runs[0]["rows_attempted"] == 5166
    assert runs[0]["rows_new"] == 400


# --- 3.3 / 3.4: redundant streak + fleet yield ---------------------------------

def test_redundant_streak_detected_at_n(session):
    pol = _mk_policy(session, "frozen",
                     date_policy={"mode": "explicit", "start": "2026-01-01",
                                  "end": "2026-01-01"})
    for i in range(3):
        _mk_run(session, pol, "redundant",
                finished=dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=i),
                plan_cells=10, rows_attempted=10, rows_new=0)
    streaks = snapshot.redundant_streaks(session, n=3)
    assert [s["policy"] for s in streaks] == ["frozen"]
    assert streaks[0]["date_policy"]["mode"] == "explicit"


def test_intermittent_redundancy_not_surfaced(session):
    pol = _mk_policy(session, "flaky")
    for i, status in enumerate(["redundant", "success", "redundant"]):
        _mk_run(session, pol, status,
                finished=dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=i))
    assert snapshot.redundant_streaks(session, n=3) == []


def test_fleet_yield_sums_rows_new_in_window(session):
    pol = _mk_policy(session, "y2")
    _mk_run(session, pol, "success", finished=dt.datetime.now(dt.timezone.utc),
            rows_attempted=100, rows_new=40)
    _mk_run(session, pol, "redundant", finished=dt.datetime.now(dt.timezone.utc),
            rows_attempted=60, rows_new=0)
    fy = snapshot.fleet_yield(session, hours=24)
    assert fy["rows_new"] == 40 and fy["rows_attempted"] == 160 and fy["runs"] == 2


# --- 4.1: frozen recurring policy refused, backfill path open -------------------

def test_frozen_window_refusal_detail():
    from fd_open_data_mcp.refresh.reconciler import _frozen_window
    pol = CrawlPolicy(
        name="f", concept_ids=[1], entity_type="fund", cron_expr="0 6 * * *",
        date_policy={"mode": "explicit", "start": "2026-08-01", "end": "2026-08-07"},
    )
    detail = _frozen_window(pol, dt.date(2026, 8, 27))
    assert detail is not None
    assert "trailing" in detail and "since_last" in detail
    # today / future / rolling windows are fine
    assert _frozen_window(CrawlPolicy(
        name="t", concept_ids=[1], entity_type="fund", cron_expr="0 6 * * *",
        date_policy={"mode": "explicit", "start": "2026-08-01", "end": "2026-08-27"},
    ), dt.date(2026, 8, 27)) is None
    assert _frozen_window(CrawlPolicy(
        name="s", concept_ids=[1], entity_type="fund", cron_expr="0 6 * * *",
        date_policy={"mode": "since_last"},
    ), dt.date(2026, 8, 27)) is None


def test_reconciler_refuses_frozen_recurring_policy(session, no_redis, stub_probe):
    from fd_open_data_mcp.refresh.reconciler import reconcile_once

    class _Never:
        def launch(self, plan, policy):
            raise AssertionError("a frozen policy must not launch")

        def poll(self, job_ref):
            return "unknown"

    pol = _mk_policy(session, "frozen-recurring",
                     date_policy={"mode": "explicit", "start": "2026-08-01",
                                  "end": "2026-08-07"},
                     cron_expr="* * * * *",
                     # created "2 minutes ago" so the minutely cron is due NOW
                     created_at=dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=2))
    session.commit()
    now = dt.datetime.now(dt.timezone.utc)
    summary = reconcile_once(session, _Never(), now=now)
    assert summary["refused"], "the cron path must refuse a frozen window"
    run = session.query(PolicyRun).filter_by(policy_id=pol.id).one()
    assert run.status == "failed" and run.detail.startswith("refused:")

"""Missed-run board tests (panel-ops-console, spec crawl-control-center).

Covers: a silently-skipped nightly policy is flagged with fire time + reason;
the flag clears after a successful run or disabling; grace interval and the
single-flight / plan-refused reason heuristics.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from fd_open_data_mcp.models import CrawlPolicy, PolicyRun
from fd_open_data_mcp.visibility.snapshot import missed_runs

NOW = datetime(2025, 6, 15, 12, 0, tzinfo=timezone.utc)


def _client():
    from fd_open_data_mcp.panel.app import app
    return TestClient(app)


def _policy(session, **over) -> CrawlPolicy:
    kw = dict(name="nightly", enabled=True, concept_ids=[1], entity_type="stock",
              date_policy={"mode": "trailing", "days": 1}, frequency="daily",
              mode="per_date", cron_expr="0 2 * * *", timezone="UTC",
              last_run_at=NOW - timedelta(days=3),  # last ran 3 days ago
              created_at=NOW - timedelta(days=30))
    kw.update(over)
    p = CrawlPolicy(**kw)
    session.add(p)
    session.commit()
    return p


def test_skipped_nightly_policy_is_flagged(session):
    _policy(session)  # 02:00 nightly cron, last run 3 days ago
    flags = missed_runs(session, now=NOW)
    assert len(flags) == 1
    f = flags[0]
    assert f["policy"] == "nightly"
    assert f["reason"] == "never launched"
    assert f["minutes_late"] > 30
    # the flagged fire is the FIRST uncovered fire: 02:00 the day after the
    # last run (2025-06-12T12:00Z) — 2025-06-13T02:00Z
    assert f["missed_fire"].startswith("2025-06-13T02:00")


def test_flag_clears_after_successful_run(session):
    p = _policy(session)
    session.add(PolicyRun(policy_id=p.id, status="success",
                          started_at=NOW - timedelta(hours=1)))
    session.commit()
    assert missed_runs(session, now=NOW) == []


def test_flag_clears_when_disabled(session):
    _policy(session, enabled=False)
    assert missed_runs(session, now=NOW) == []


def test_reason_plan_refused_keeps_flag(session):
    p = _policy(session)
    # yesterday's fire launched and was refused: run recorded, flag stays
    session.add(PolicyRun(policy_id=p.id, status="failed",
                          started_at=NOW - timedelta(days=1),
                          detail="refused: estimated 99999 fetches exceeds "
                                 "POLICY_MAX_FETCHES=50000"))
    session.commit()
    flags = missed_runs(session, now=NOW)
    assert len(flags) == 1 and flags[0]["reason"] == "plan refused"


def test_reason_blocked_by_single_flight(session):
    p = _policy(session)
    # an OPEN run started before the latest fire: the reconciler keeps skipping
    session.add(PolicyRun(policy_id=p.id, status="running",
                          started_at=NOW - timedelta(days=3)))
    session.commit()
    flags = missed_runs(session, now=NOW)
    assert len(flags) == 1 and flags[0]["reason"] == "blocked by single-flight"


def test_grace_window_hides_recent_fire(session):
    # last run covered through yesterday; the first uncovered fire (today
    # 02:00) is only 10h stale — inside the nightly grace (2×24h), so quiet
    p = _policy(session, cron_expr="0 2 * * *",
                last_run_at=NOW - timedelta(days=1))
    session.add(PolicyRun(policy_id=p.id, status="success",
                          started_at=NOW - timedelta(days=1)))
    session.commit()
    assert missed_runs(session, now=NOW) == []
    # with a tighter explicit grace the same state flags
    flags = missed_runs(session, now=NOW, grace_min=30)
    assert len(flags) == 1 and flags[0]["grace_min"] == 30


def test_partial_renders_flags_and_degrades(session):
    _policy(session)
    r = _client().get("/panel/partials/missed")
    assert r.status_code == 200
    assert "nightly" in r.text and "never launched" in r.text
    # home page polls the missed partial
    home = _client().get("/panel")
    assert "/panel/partials/missed" in home.text and "Missed runs" in home.text

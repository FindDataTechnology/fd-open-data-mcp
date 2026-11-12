"""Home cockpit tests (panel-ui-refresh Phase 1): KPI row, yield trend,
running-run progress. Spec: cockpit home — KPIs reflect the last 24h,
trend points carry inspectable titles, running runs show progress."""
from __future__ import annotations

import datetime as dt

from fastapi.testclient import TestClient
from sqlalchemy import select

from fd_open_data_mcp.db import get_database
from fd_open_data_mcp.models import CrawlPolicy, PolicyRun
from fd_open_data_mcp.panel.app import app, kpi_snapshot

client = TestClient(app)


def _policy(name="kpi"):
    s = get_database().get_session()
    try:
        p = CrawlPolicy(name=name, enabled=True, concept_ids=[1], entity_type="fund",
                        cron_expr="0 6 * * *", timezone="UTC",
                        date_policy={"mode": "since_last"}, frequency="daily",
                        mode="per_date")
        s.add(p)
        s.commit()
        return p.id
    finally:
        s.close()


def _run(pid, status, finished=None, started=None, rows_new=None,
         rows_attempted=None, plan_cells=None):
    now = dt.datetime.utcnow()
    return PolicyRun(
        policy_id=pid, status=status,
        started_at=started or (now - dt.timedelta(hours=2)),
        finished_at=finished, rows_new=rows_new, rows_attempted=rows_attempted,
        plan_cells=plan_cells)


def test_kpi_snapshot_reflects_24h_window(session):
    s = get_database().get_session()
    try:
        pid = _policy("kpi-window")
        now = dt.datetime.utcnow()
        in_window = [
            _run(pid, "success", finished=now - dt.timedelta(hours=1), rows_new=3000),
            _run(pid, "success", finished=now - dt.timedelta(hours=2), rows_new=9400),
            _run(pid, "success", finished=now - dt.timedelta(hours=3), rows_new=100),
            _run(pid, "success", finished=now - dt.timedelta(hours=4), rows_new=0),
            _run(pid, "success", finished=now - dt.timedelta(hours=5), rows_new=0),
            _run(pid, "success", finished=now - dt.timedelta(hours=6), rows_new=0),
            _run(pid, "zero_yield", finished=now - dt.timedelta(hours=7)),
            _run(pid, "failed", finished=now - dt.timedelta(hours=8)),
            _run(pid, "running"),
        ]
        # outside the window: must not count
        stale = _run(pid, "failed", finished=now - dt.timedelta(hours=30), rows_new=999)
        s.add_all(in_window + [stale])
        s.commit()
    finally:
        s.close()

    s = get_database().get_session()
    try:
        k = kpi_snapshot(s)
        assert k["running"] == 1
        assert k["success_rate"] == 75  # 6 success / 8 decidable
        assert k["new_rows"] == 12500
        assert k["failures"] == 2  # failed + zero_yield, stale excluded
        assert k["no_signal"] == 0
        assert k["unknown"] == 0
    finally:
        s.close()

    text = client.get("/panel").text
    assert "75%" in text
    assert "12,500" in text


def test_success_rate_excludes_no_signal_runs(session):
    """口径：no_op / redundant 是"这一轮没有信号"，不是失败。

    这条是**负例**：若有人把分母改回"所有非 running 终态"，这里会变成 50%
    （1 success / 2 终态）而不是 100%。线上真实形状就是这一类
    （failed 3 / no_op 3 / success 5 → 旧口径 45.5%，可判定口径 62.5%）。
    """
    s = get_database().get_session()
    try:
        pid = _policy("kpi-no-signal")
        now = dt.datetime.utcnow()
        s.add_all([
            _run(pid, "success", finished=now - dt.timedelta(hours=1), rows_new=10),
            _run(pid, "no_op", finished=now - dt.timedelta(hours=2)),
            _run(pid, "redundant", finished=now - dt.timedelta(hours=3)),
        ])
        s.commit()
    finally:
        s.close()

    s = get_database().get_session()
    try:
        k = kpi_snapshot(s)
        assert k["success_rate"] == 100, "no_op/redundant 不得进分母"
        assert k["no_signal"] == 2, "但必须计数，不能悄悄消失"
        assert k["failures"] == 0
    finally:
        s.close()


def test_success_rate_counts_failure_and_surfaces_unknown(session):
    """真失败要进分母；未登记状态不进分母但必须显式计数（否则静默消失）。"""
    s = get_database().get_session()
    try:
        pid = _policy("kpi-fail-unknown")
        now = dt.datetime.utcnow()
        s.add_all([
            _run(pid, "success", finished=now - dt.timedelta(hours=1), rows_new=1),
            _run(pid, "failed", finished=now - dt.timedelta(hours=2)),
            _run(pid, "partial", finished=now - dt.timedelta(hours=3)),
        ])
        s.commit()
    finally:
        s.close()

    s = get_database().get_session()
    try:
        k = kpi_snapshot(s)
        assert k["success_rate"] == 50, "1 success / (1 success + 1 failed)"
        assert k["failures"] == 1
        assert k["unknown"] == 1, "未登记的状态必须暴露出来"
    finally:
        s.close()


def test_kpi_snapshot_empty_db(session):
    s = get_database().get_session()
    try:
        k = kpi_snapshot(s)
        assert k == {"running": 0, "success_rate": None, "new_rows": 0,
                     "failures": 0, "no_signal": 0, "unknown": 0}
    finally:
        s.close()
    # home still renders with an em-dash instead of a rate
    assert client.get("/panel").status_code == 200


def test_home_trend_chart_has_titled_points(session):
    s = get_database().get_session()
    try:
        pid = _policy("trend")
        now = dt.datetime.utcnow()
        s.add_all([
            _run(pid, "success", finished=now - dt.timedelta(days=1), rows_new=120),
            _run(pid, "success", finished=now - dt.timedelta(days=1), rows_new=30),
            _run(pid, "success", finished=now - dt.timedelta(days=3), rows_new=45),
        ])
        s.commit()
    finally:
        s.close()

    from fd_open_data_mcp.panel.app import yield_trend
    s = get_database().get_session()
    try:
        g = yield_trend(s)
        assert sum(b["value"] for b in g["bars"]) == 195  # 120+30+45, zero-filled
        tall = [b for b in g["bars"] if b["bh"] > 0]
        assert len(tall) == 2  # day-1 and day-3 render nonzero bars
    finally:
        s.close()

    # single-language chrome (panel-rbac-i18n-refresh): zh renders only
    # Chinese, the en locale renders the English heading
    text = client.get("/panel").text
    assert "产出趋势" in text and "Yield trend" not in text
    assert "Yield trend" in client.get(
        "/panel", cookies={"panel-lang": "en"}).text
    assert "<title>" in text  # per-point values inspectable without JS
    assert 'class="bar"' in text


def test_running_partial_shows_progress(session):
    s = get_database().get_session()
    try:
        pid = _policy("prog")
        s.add(_run(pid, "running", rows_attempted=25, plan_cells=100, rows_new=10))
        s.commit()
    finally:
        s.close()

    r = client.get("/panel/partials/running")
    assert r.status_code == 200
    assert "progress-svg" in r.text
    # numeric counters stay alongside the bar
    assert "25" in r.text and "100" in r.text and "10" in r.text

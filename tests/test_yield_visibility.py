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


def test_fleet_yield_does_not_count_skip_records_as_runs(session):
    """`skipped` 是「没派发」的留痕，不是运行次数（unblock-silent-dispatch-stalls 3.3）。

    一条卡住的 open run 会让 reconciler **每跳**落一行 skipped（15 分钟一跳 ⇒
    一天 96 行），旧判据 `status != "running"` 把它们全数进 runs ⇒ 日报会写
    「今天 96 次运行」，而实际一次都没派出去。`stalled` 仍计数（它真跑过）。
    """
    pol = _mk_policy(session, "y3")
    _mk_run(session, pol, "success", finished=dt.datetime.now(dt.timezone.utc),
            rows_attempted=100, rows_new=40)
    for _ in range(3):
        _mk_run(session, pol, "skipped")
    _mk_run(session, pol, "stalled")
    _mk_run(session, pol, "running")
    fy = snapshot.fleet_yield(session, hours=24)
    assert fy["runs"] == 2, fy          # success + stalled；skipped/running 不算
    assert fy["rows_new"] == 40 and fy["rows_attempted"] == 100


# --- split-fleet-yield-by-executor --------------------------------------------
# 「这个总数是什么」与「它由谁产生」是两件事：direct 脚本全量 upsert，把同一个
# 计数写进两列（实测 30 天 97/97 轮 attempted==new），它们的份额是**重写行**。
# 实测窗口里 direct 占这个总和的 99.3%，所以任何把它叫「新增行」的读法虚高两个数量级。

def test_fleet_yield_splits_by_executor(session):
    crawl = _mk_policy(session, "crawl-pol", executor="scrapy")
    snap = _mk_policy(session, "snap-pol", executor="direct")
    now = dt.datetime.now(dt.timezone.utc)
    _mk_run(session, crawl, "success", finished=now, rows_attempted=5, rows_new=5)
    _mk_run(session, snap, "success", finished=now, rows_attempted=100, rows_new=100)

    fy = snapshot.fleet_yield(session, hours=24)

    assert fy["rows_new"] == 105
    assert fy["by_executor"] == {"scrapy": 5, "direct": 100}
    assert fy["rows_new_unattributed"] == 0
    # 不变式：分桶加上未归属必须等于总数（否则总数与它的解释各说各话）
    assert sum(fy["by_executor"].values()) + fy["rows_new_unattributed"] == fy["rows_new"]


def test_fleet_yield_keeps_runs_without_a_policy_row(session):
    """没有策略行的轮次不能从这个「有没有东西落地」的数里消失。

    形状按真实库来：`policy_id` 是 `ON DELETE CASCADE` 的外键，所以「策略被删、
    run 留下」在 schema 上不可能；真实人口是 `policy_id IS NULL`（2026-11-16 活库
    实测 2 行，其中 1 行带 rows_new）。第一版用例插了个悬空 id=99999，夹具的 sqlite
    开着外键 ⇒ 直接 IntegrityError——那是我自己造的不可构造形状。
    红证：把 outerjoin 改成 innerjoin，这一条会红（总数与分桶同时少掉 7）。"""
    now = dt.datetime.now(dt.timezone.utc)
    session.add(PolicyRun(policy_id=None, status="success", started_at=now,
                          finished_at=now, rows_attempted=7, rows_new=7))
    session.commit()

    fy = snapshot.fleet_yield(session, hours=24)

    assert fy["rows_new"] == 7
    assert fy["by_executor"] == {}
    assert fy["rows_new_unattributed"] == 7
    assert sum(fy["by_executor"].values()) + fy["rows_new_unattributed"] == fy["rows_new"]


def test_fleet_yield_zero_day_reads_zero_everywhere(session):
    """spec 场景「A zero-acquisition day is visible」加上分桶这一半：总数是 0 时
    每个桶也必须是 0，不许把上一轮的桶留在这个总数的解释里。"""
    pol = _mk_policy(session, "dead")
    now = dt.datetime.now(dt.timezone.utc)
    for st in ("zero_yield", "no_op", "redundant"):
        _mk_run(session, pol, st, finished=now, rows_attempted=10, rows_new=0)

    fy = snapshot.fleet_yield(session, hours=24)

    assert fy["rows_new"] == 0
    assert fy["by_executor"] == {"scrapy": 0}
    assert fy["rows_new_unattributed"] == 0


def test_digest_yield_line_names_what_it_counts():
    """文案是这一笔的交付物本身：说「new rows」就是在陈述一个假事实。"""
    from fd_open_data_mcp.visibility.digest import _yield_line

    line = _yield_line({"rows_new": 105, "rows_attempted": 105, "runs": 2,
                        "by_executor": {"scrapy": 5, "direct": 100},
                        "rows_new_unattributed": 0})

    assert "105 rows landed" in line
    assert "scrapy 5" in line and "direct 100" in line
    assert "new rows" not in line          # 红证：退回旧串后这条必红
    assert "unattributed" not in line      # 为 0 时不念，免得教人期待一个常空桶


def test_digest_yield_line_surfaces_the_unattributed_bucket():
    from fd_open_data_mcp.visibility.digest import _yield_line

    line = _yield_line({"rows_new": 12, "rows_attempted": 12, "runs": 2,
                        "by_executor": {"scrapy": 5},
                        "rows_new_unattributed": 7})

    assert "12 rows landed" in line
    assert ", unattributed 7" in line


def test_digest_yield_line_survives_a_payload_without_the_split():
    """旧快照字典（或手工构造的 payload）不能让 digest 崩：全部走 .get 缺省。"""
    from fd_open_data_mcp.visibility.digest import _yield_line

    line = _yield_line({"rows_new": 0, "rows_attempted": 0, "runs": 0})

    assert "0 rows landed" in line
    assert "no runs with counters" in line


def test_digest_body_actually_renders_the_new_yield_line():
    """在**消息正文那一层**断言，不只测辅助函数。

    红证实测（2026-11-17）：只把 `format_digest` 里那处调用点退回旧串、留着
    `_yield_line` 不动，三个直测辅助函数的用例**全绿**——它们证明不了正文用的是
    新文案。这一条才是承重的：退回调用点它就红。
    """
    from zoneinfo import ZoneInfo

    from fd_open_data_mcp.visibility.digest import format_digest

    snap = {
        "generated_at": "2026-11-17T00:00:00",
        "recent_runs": [],
        "per_source_outcome": [],
        "circuit_state": [],
        "fleet": [],
        "fleet_yield": {"rows_new": 105, "rows_attempted": 105, "runs": 2,
                        "by_executor": {"scrapy": 5, "direct": 100},
                        "rows_new_unattributed": 0},
    }
    _title, body = format_digest(snap, ZoneInfo("UTC"))

    assert "105 rows landed" in body
    assert "scrapy 5" in body and "direct 100" in body
    assert "new rows" not in body


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

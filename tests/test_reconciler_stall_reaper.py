"""卡死回收与跳过留痕（openspec change: unblock-silent-dispatch-stalls）。

**这两条都是"静默停更"的解药**，所以每条正向断言都配破坏实验：

- 卡死回收：`poll()` 在 Job 被 ttl/backoff 回收后返回 `"unknown"`，而完成探针只在
  `{success, failed, deadline_exceeded}` 时关行——`"unknown"` 永远不关行。
  2026-11-13 生产实历：run id=2383 挂在 `running` 3.6 小时，Job 的
  activeDeadlineSeconds 是 3 小时；派发是单飞的，于是当天 18:17 的计划内执行
  **连行都没留**，且跳过只写日志（pod 已回收，日志也抓不到）。
- 跳过留痕：`skipped` 是**记录**不是**运行**——不参与单飞、不计尝试与产出，
  否则跳过会自锁或污染统计。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from fd_open_data_mcp.models import CrawlPolicy, PolicyRun
from fd_open_data_mcp.refresh import reconciler
from fd_open_data_mcp.refresh.reconciler import (
    SKIPPED, STALLED, reconcile_once,
)

NOW = datetime(2026, 11, 13, 12, 0, tzinfo=timezone.utc)


class _Launcher:
    """最小 launcher：可指定 poll 读数，记录 launch 次数。"""

    def __init__(self, state="unknown"):
        self.state = state
        self.launched = 0

    def launch(self, plan, policy):
        self.launched += 1
        return (f"job-{self.launched}", None)

    def poll(self, job_ref):
        return self.state

    def diagnose(self, job_ref):
        return None


def _policy(session, *, name="p1", cron="0 * * * *", enabled=True) -> CrawlPolicy:
    p = CrawlPolicy(
        name=name, enabled=enabled, concept_ids=[], entity_type="stock",
        entity_ids=[1], date_policy={"mode": "explicit", "start": "2025-06-01"},
        frequency="daily", mode="per_date", cron_expr=cron, timezone="UTC",
        last_run_at=NOW - timedelta(hours=2),   # 每小时 cron → NOW 到期
    )
    session.add(p)
    session.commit()
    return p


def _open_run(session, policy, *, hours_ago: float, job_ref="gone-job") -> PolicyRun:
    run = PolicyRun(policy_id=policy.id, status="running", job_ref=job_ref,
                    started_at=NOW - timedelta(hours=hours_ago))
    session.add(run)
    session.commit()
    return run


# ── 阈值来源（task 1.2）──────────────────────────────────────


def test_threshold_is_derived_from_executor_deadline():
    """阈值**派生**自执行侧，不是复制：调执行侧 env，阈值必须跟着动。"""
    base = reconciler.policy_run_max_age_seconds()
    assert base == reconciler._active_deadline_seconds() + reconciler.RECONCILER_TICK_SECONDS


def test_threshold_follows_executor_env(monkeypatch):
    monkeypatch.setenv(reconciler.JOB_ACTIVE_DEADLINE_ENV, "600")
    assert reconciler._active_deadline_seconds() == 600
    assert reconciler.policy_run_max_age_seconds() == 600 + reconciler.RECONCILER_TICK_SECONDS


# ── 卡死回收（task 2.1–2.5）──────────────────────────────────


def test_unknown_state_past_ceiling_is_stalled_and_releases_single_flight(session):
    """核心用例：Job 读不到 + 超龄 → 判 stalled，且同一跳就能重新派发。"""
    p = _policy(session)
    run = _open_run(session, p, hours_ago=4)          # > 3h15m 上限
    launcher = _Launcher(state="unknown")

    summary = reconcile_once(session, launcher, now=NOW)

    session.refresh(run)
    assert run.status == STALLED
    assert run.finished_at is not None
    assert "stalled" in (run.detail or "")
    assert "unresponsive" in (run.detail or "").lower() or "unresolvable" in (run.detail or "")
    assert summary["stalled"] == 1
    # 同一跳即恢复派发（不是下一跳）
    assert summary["launched"] and summary["launched"][0]["policy"] == "p1"


def test_unknown_state_under_ceiling_is_not_reaped(session):
    """未超龄不回收：它可能只是刚起来，误收会双跑。"""
    p = _policy(session)
    run = _open_run(session, p, hours_ago=1)
    launcher = _Launcher(state="unknown")

    summary = reconcile_once(session, launcher, now=NOW)

    session.refresh(run)
    assert run.status == "running"
    assert summary["stalled"] == 0
    assert summary["launched"] == []
    assert summary["skipped"][0]["policy"] == "p1"


def test_running_state_past_ceiling_is_not_reaped(session):
    """`running` 是 Job 系统在说"它还活着"——不回收，交给 activeDeadline 处理。

    误收一个真在跑的任务会双跑，比晚收更坏。
    """
    p = _policy(session)
    run = _open_run(session, p, hours_ago=10)
    launcher = _Launcher(state="running")

    summary = reconcile_once(session, launcher, now=NOW)

    session.refresh(run)
    assert run.status == "running"
    assert summary["stalled"] == 0


def test_run_without_job_ref_is_reaped(session):
    """没有 job_ref 的行永远无法被 poll 关掉——超龄即回收。"""
    p = _policy(session)
    run = _open_run(session, p, hours_ago=5, job_ref=None)
    launcher = _Launcher(state="unknown")

    summary = reconcile_once(session, launcher, now=NOW)

    session.refresh(run)
    assert run.status == STALLED
    assert summary["stalled"] == 1


def test_stall_never_fabricates_a_result(session):
    """回收不编造产出：stalled 绝不能写成 success/zero_yield，也不填产出数。"""
    p = _policy(session)
    run = _open_run(session, p, hours_ago=4)

    reconcile_once(session, _Launcher(state="unknown"), now=NOW)

    session.refresh(run)
    assert run.status == STALLED
    assert run.status not in ("success", "zero_yield", "no_op", "redundant")
    assert run.rows_attempted is None and run.rows_new is None


def test_reaper_is_the_only_reason_it_stops_blocking(session, monkeypatch):
    """破坏实验：把阈值调到极大（等于关掉回收）→ 同一场景必须退回"永久阻塞"。"""
    p = _policy(session)
    run = _open_run(session, p, hours_ago=4)
    monkeypatch.setattr(reconciler, "policy_run_max_age_seconds", lambda: 10 ** 9)

    summary = reconcile_once(session, _Launcher(state="unknown"), now=NOW)

    session.refresh(run)
    assert run.status == "running"
    assert summary["launched"] == []
    assert summary["skipped"], "关掉回收后它应该继续被单飞挡住 —— 证明正向用例绑在回收上"


# ── 跳过留痕（task 3.1–3.4）─────────────────────────────────


def test_single_flight_skip_leaves_a_durable_row(session):
    p = _policy(session)
    blocker = _open_run(session, p, hours_ago=1)     # 未超龄，不回收

    summary = reconcile_once(session, _Launcher(state="unknown"), now=NOW)

    skips = session.query(PolicyRun).filter_by(policy_id=p.id, status=SKIPPED).all()
    assert len(skips) == 1
    assert str(blocker.id) in (skips[0].detail or "")
    assert summary["skipped"][0]["blocked_by_run"] == blocker.id


def test_skip_rows_do_not_self_lock(session):
    """两跳都跳过：第二跳必须有自己的 skipped 行，而不是被第一跳的挡住。"""
    p = _policy(session)
    _open_run(session, p, hours_ago=1)

    reconcile_once(session, _Launcher(state="unknown"), now=NOW)
    reconcile_once(session, _Launcher(state="unknown"), now=NOW + timedelta(hours=1))

    assert session.query(PolicyRun).filter_by(policy_id=p.id, status=SKIPPED).count() == 2


def test_skip_rows_are_not_open_runs(session):
    """`skipped` 不是 `_OPEN`：单飞与容量统计都不能把它算进去。"""
    p = _policy(session)
    session.add(PolicyRun(policy_id=p.id, status=SKIPPED,
                          started_at=NOW, finished_at=NOW, detail="earlier skip"))
    session.commit()

    assert reconciler._open_run(session, p.id) is None
    summary = reconcile_once(session, _Launcher(state="success"), now=NOW)
    assert summary["launched"], "只有 skipped 行时不该被挡住"


def test_skip_does_not_count_as_attempt_or_yield(session):
    """跳过不计尝试/产出：否则统计里会出现"跑了但零产出"的假象。"""
    p = _policy(session)
    _open_run(session, p, hours_ago=1)

    reconcile_once(session, _Launcher(state="unknown"), now=NOW)

    skip = session.query(PolicyRun).filter_by(policy_id=p.id, status=SKIPPED).one()
    assert skip.rows_attempted is None
    assert skip.rows_new is None
    assert skip.plan_json is None


def test_skip_row_only_appears_when_single_flight_actually_blocks(session, monkeypatch):
    """因果证明：`skipped` 行只由单飞分支产生，不是顺手写出来的。

    把 `_open_run` 改成"看不到阻塞"（等价于没有单飞），断言这一跳既不写 skipped、
    也正常派发——说明那条记录确实绑在"被挡住"这个事实上。
    """
    p = _policy(session)
    _open_run(session, p, hours_ago=1)
    monkeypatch.setattr(reconciler, "_open_run", lambda s, pid: None)

    summary = reconcile_once(session, _Launcher(state="success"), now=NOW)

    assert session.query(PolicyRun).filter_by(policy_id=p.id, status=SKIPPED).count() == 0
    assert summary["launched"], "没有阻塞时应当正常派发"


def test_threshold_zero_reaps_a_young_run(session, monkeypatch):
    """破坏实验（任务 2.5 指定形态）：阈值调成 0 → 刚起的行也被判卡死。

    断言的就是"回收确实按阈值在判"，而不是碰巧把老行关了：同一份 1 小时前的行，
    在默认阈值下保留、在 0 阈值下被收——两边的差异只能来自阈值。
    """
    monkeypatch.setattr(reconciler, "policy_run_max_age_seconds", lambda: 0)
    p = _policy(session)
    run = _open_run(session, p, hours_ago=1)      # 默认阈值下必然保留

    reconcile_once(session, _Launcher(state="unknown"), now=NOW)

    session.refresh(run)
    assert run.status == STALLED, "阈值 0 时它必须被收掉 —— 否则回收没绑在阈值上"


# ── 可查可过滤（task 3.4）───────────────────────────────────


def test_panel_runs_view_offers_the_new_statuses():
    """panel 的 runs 视图必须给 stalled/skipped 一个筛选入口。

    记录写进去了但界面过滤不到，等于运维还是看不见——这正是"跳过只写日志"的第二形态。
    """
    from pathlib import Path

    tpl = (Path(__file__).resolve().parents[1]
           / "fd_open_data_mcp/panel/templates/partial_runs_results.html")
    body = tpl.read_text(encoding="utf-8")
    assert "'stalled'" in body
    assert "'skipped'" in body

"""`scripts/check_policy_freshness.py` 的判据测试。

这条闸防的是 2026-11-10 那类「每天跑、每天失败、没有一条通道会说话」：policy 2659
连续三天 `failed`，detail 是「failed before the runner checked in；pod 已回收，无终态
可抓」，而 k8s 作业闸按 CronJob 枚举看不见它、爬虫闸按 `crawl_runs` 枚举也没有它的行。

判据必须有正反两面：只有「能红」的闸在正常策略上误报，只有「能绿」的闸永远不会响。
"""
from __future__ import annotations

import importlib.util
import os
import sys
from datetime import datetime, timedelta, timezone

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO, "scripts", "check_policy_freshness.py")

NOW = datetime(2026, 11, 14, 12, 0, tzinfo=timezone.utc)


def _load():
    name = "policy_freshness_gate"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


gate = _load()


def _run(status: str, hours_ago: float, detail: str = ""):
    return gate.PolicyRun(
        status=status,
        started_at=NOW - timedelta(hours=hours_ago),
        detail=detail,
    )


def _state(pid: int, runs, name: str = "pol", cron: str | None = "0 6 * * *",
           tz: str | None = "UTC"):
    return gate.PolicyState(pid, name, cron, tz, runs)


def _judge(states, **kw):
    und = kw.pop("undetermined", None)
    if und is None:
        und = []
    findings, facts = gate.judge(states, now=NOW, undetermined=und, **kw)
    return findings, facts, und


def _rules(findings):
    return {(f.policy_id, f.rule) for f in findings}


# ---------------------------------------------------------------- 红路

def test_latest_failed_counts():
    f, _, _ = _judge([_state(1, [_run("failed", 26, "eastmoney refused the connection")])])
    assert (1, "latest_failed") in _rules(f)


def test_stalled_counts_too():
    """`stalled` 是「执行器没说话」，它同样是这一轮没产出，必须与 failed 同档。"""
    f, _, _ = _judge([_state(1, [_run("stalled", 8, "manual repair: upstream refused")])])
    assert (1, "latest_failed") in _rules(f)


def test_opaque_reason_counts_when_detail_says_nothing():
    """11-10..11-12 的形态：详情只有「pod 已回收、无终态可抓」。"""
    detail = ("job cheap/crawl-policy-2659-20261110102204 failed before the runner "
              "checked in; pod 已回收，无终态可抓")
    f, _, _ = _judge([_state(1, [_run("failed", 24, detail)])])
    rules = _rules(f)
    assert (1, "latest_failed") in rules
    assert (1, "opaque_reason") in rules


def test_empty_detail_is_opaque():
    f, _, _ = _judge([_state(1, [_run("failed", 24, "")])])
    assert (1, "opaque_reason") in _rules(f)


def test_a_real_reason_is_not_opaque():
    """2659 在 11-14 的形态：detail 写清了「东财对 cheap9 出口 IP 限流」——
    已经能判方向了，不该再报「没留原因」，否则我们把已经修好的留痕又说成坏的。"""
    f, _, _ = _judge([_state(1, [_run("stalled", 7, "东财对 cheap9 出口 IP 限流，本轮 3 票连续被拒")])])
    rules = _rules(f)
    assert (1, "latest_failed") in rules
    assert (1, "opaque_reason") not in rules


def test_dangling_open_run_counts():
    """2026-11-13 实历：一条 running 行挂了 5.2 小时，整天没有新行。"""
    f, _, _ = _judge([_state(1, [_run("running", 6.0)])])
    assert (1, "dangling_open_run") in _rules(f)


def test_a_fresh_open_run_is_not_dangling():
    """执行器上限 3h；刚起 10 分钟的行是在跑，不是悬着（误收会双跑）。"""
    f, facts, _ = _judge([_state(1, [_run("running", 10 / 60)])])
    assert f == []
    assert any("在跑" in line for line in facts)


def test_dangling_does_not_also_report_never_ran():
    """同一处问题只记一次：悬着的行已经解释了「为什么没有终态行」。"""
    f, _, _ = _judge([_state(1, [_run("running", 6.0)])])
    assert (1, "never_ran") not in _rules(f)


def test_never_ran_when_no_records_at_all():
    f, _, _ = _judge([_state(1, [])])
    assert (1, "never_ran") in _rules(f)


def test_no_recent_run_judges_the_last_scheduled_fire():
    """判据是「最近一次**应触发时刻**有没有产出」，不是「多少天没跑」。

    NOW = 2026-11-14 12:00Z（周六）。`17 18 * * 1-5` @Asia/Shanghai 的最近一次应触发
    是周五 10:17Z：周五那轮有记录 ⇒ 绿；记录停在周三 ⇒ 漏了周五整轮 ⇒ 红。
    """
    # 25h 前 = 11-13 11:00Z，晚于周五那轮 10:17Z ⇒ 绿
    fresh, _, _ = _judge([_state(1, [_run("success", 25)],
                                 cron="17 18 * * 1-5", tz="Asia/Shanghai")])
    assert (1, "no_recent_run") not in _rules(fresh)

    # 48h 前 = 11-12 12:00Z，早于周五那轮 ⇒ 周五整轮没有记录 ⇒ 红
    missed, _, _ = _judge([_state(1, [_run("success", 48)],
                                  cron="17 18 * * 1-5", tz="Asia/Shanghai")])
    assert (1, "no_recent_run") in _rules(missed)


def test_a_weekend_alone_does_not_make_a_weekday_policy_red():
    """周六/周日不该因为「昨天没跑」而红：最近一次应触发仍是周五那轮。"""
    f, _, _ = _judge([_state(1, [_run("success", 2)],      # 今天 10:00Z 跑过
                             cron="17 18 * * 1-5", tz="Asia/Shanghai")])
    assert f == []


def test_a_bad_cron_is_undetermined_not_a_fact():
    """解析不出要退 2。把它当「没排期」静默放过，一次格式变更就能让整条判据消失。"""
    _, _, und = _judge([_state(1, [_run("success", 1)], cron="every tuesday")])
    assert len(und) == 1


def test_a_bad_timezone_is_undetermined():
    _, _, und = _judge([_state(1, [_run("success", 1)], tz="Mars/Olympus")])
    assert len(und) == 1


# ---------------------------------------------------------------- 绿路 / 合法态

def test_healthy_policy_is_green():
    f, _, _ = _judge([_state(1, [_run("success", 3)], cron="17 18 * * 1-5", tz="Asia/Shanghai")])
    assert f == []


@pytest.mark.parametrize("status", ["no_op", "redundant", "skipped"])
def test_non_yield_outcomes_are_facts_not_hits(status):
    """月度策略「本期还没结束」是设计内的不产出；把它算成失败会让闸常年红。"""
    f, facts, _ = _judge([_state(1, [_run(status, 2, "next period has not finished")],
                                 cron="0 6 1 * *")])
    assert f == []
    assert any(status in line for line in facts)


def test_no_cron_is_a_fact_not_a_hit():
    f, facts, _ = _judge([_state(1, [_run("success", 400)], cron=None)])
    assert f == []
    assert any("不排期" in line for line in facts)


def test_self_test_passes():
    assert gate._self_test() == 0


# ---------------------------------------------------------------- 红证（报告路径）

def test_inject_failed_makes_a_healthy_policy_red():
    states = [_state(5, [_run("success", 5)], name="fund-etf-lof-ohlcv")]
    gate._inject(states, assume_failed="p5", assume_missed_slot=None, now=NOW)
    f, _, _ = _judge(states)
    assert (5, "latest_failed") in _rules(f)


def test_inject_missed_slot_drops_this_rounds_run():
    states = [_state(8, [_run("success", 26), _run("success", 50)],
                     name="fund-nav-daily-snapshot", cron="23 21 * * 1-6",
                     tz="Asia/Shanghai")]
    gate._inject(states, assume_failed=None, assume_missed_slot="p8", now=NOW)
    f, _, _ = _judge(states)
    assert (8, "no_recent_run") in _rules(f)


def test_inject_on_an_unknown_target_says_so():
    """注入没生效必须说出来——静默的"注入成功"会让人以为红证通过了。"""
    notes = gate._inject([_state(1, [])], assume_failed="no-such-policy",
                         assume_missed_slot=None, now=NOW)
    assert any("没有匹配" in n for n in notes)


# ---------------------------------------------------------------- 与执行侧同源

def test_executor_ceiling_comes_from_the_reconciler():
    """上限只有一个来源：判得比执行侧紧会让活着的 Job 被判死（双跑），
    松了则 Job 死了行永不关。导入得到就必须相等。"""
    if gate._active_deadline_seconds is None:
        pytest.skip("reconciler 不可导入（沙箱）")
    from fd_open_data_mcp.refresh.reconciler import (
        DEFAULT_JOB_ACTIVE_DEADLINE_SECONDS,
    )
    assert gate.DEFAULT_EXECUTOR_CEILING_SECONDS == DEFAULT_JOB_ACTIVE_DEADLINE_SECONDS


def test_executor_ceiling_honours_the_env_override(monkeypatch):
    if gate._active_deadline_seconds is None:
        pytest.skip("reconciler 不可导入（沙箱）")
    monkeypatch.setenv("SCRAW_JOB_ACTIVE_DEADLINE_SECONDS", "600")
    assert gate._active_deadline_seconds() == 600


# ---------------------------------------------------------------- 零产出（zero_yield）
#
# 2026-11-15：这个状态原先不在这道闸的任何一档里（既不在 FAILED 也不在 TERMINAL），
# 于是每条策略的「最近一次终态」都跳过它、读到上一次 success。实况是 policy 2
# `fund-pilot-aum` 在 11-08/09/10 连出三次 zero_yield，而这三天的闸输出写的是
# 「最近一次终态 success」。`reconciler.classify_yield` 把这个状态叫 the outage shape，
# 面板 cockpit 同期已把它算进 failures。

def _zy(hours_ago: float, *, cells=630, att=0, new=0, detail=""):
    return gate.PolicyRun(status="zero_yield", started_at=NOW - timedelta(hours=hours_ago),
                          detail=detail, plan_cells=cells,
                          rows_attempted=att, rows_new=new)


def _judge_one(runs, *, cron="0 6 * * *"):
    und: list[str] = []
    findings, facts = gate.judge([gate.PolicyState(1, "p", cron, "UTC", runs)],
                                 now=NOW, undetermined=und)
    return findings, facts, und


def test_zero_yield_is_a_terminal_state_not_invisible():
    """旧版最坏的那种错：最新一跳是零产出，读出来却说「最近一次终态 success」。"""
    findings, facts, _ = _judge_one([_zy(5), _run("success", 29)])
    assert not any("success" in f for f in facts), facts
    assert any("zero_yield" in f for f in facts), facts


def test_three_consecutive_zero_yields_count():
    findings, _, _ = _judge_one([_zy(5), _zy(29), _zy(53), _run("success", 77)])
    rules = [f.rule for f in findings]
    assert rules.count("repeated_zero_yield") == 1


def test_two_consecutive_zero_yields_are_a_fact_only():
    """周末连着两天是常态形态。两天就红，闸会每周红一次、然后被无视。"""
    findings, facts, _ = _judge_one([_zy(5), _zy(29), _run("success", 53)])
    assert not findings
    assert any("zero_yield" in f and "只报事实" in f for f in facts)


def test_a_single_zero_yield_is_a_fact_only():
    findings, facts, _ = _judge_one([_zy(5), _run("success", 29)])
    assert not findings
    assert any("zero_yield" in f for f in facts)


def test_the_streak_counts_terminal_rows_only():
    """中间夹一条 running 不该把连续计数打断（它自己会被 R3 单独判）。"""
    findings, _, _ = _judge_one([
        _zy(5), _run("running", 10), _zy(29), _zy(53), _run("success", 77),
    ])
    assert any(f.rule == "repeated_zero_yield" for f in findings)


def test_zero_yield_shape_names_the_two_causes():
    """两种成因的处置相反：查 pod 为什么早退 vs 查上游为什么空返回。"""
    died = gate._zero_yield_shape(gate.PolicyRun("zero_yield", NOW, plan_cells=630))
    empty = gate._zero_yield_shape(
        gate.PolicyRun("zero_yield", NOW, plan_cells=630, rows_attempted=0, rows_new=0))
    assert "首次上报前就结束" in died
    assert "上游没有给数据" in empty
    assert died != empty


def test_unknown_status_is_undetermined_not_green():
    """`aborted` 这种没有 run 侧语义的状态：既不猜成失败，也不静默成成功。"""
    findings, facts, und = _judge_one([gate.PolicyRun("aborted", NOW - timedelta(hours=5)),
                                       _run("success", 29)])
    assert not findings
    assert not any("success" in f for f in facts), facts
    assert len(und) == 1 and "aborted" in und[0]


def test_inject_zero_yield_makes_a_healthy_policy_red():
    states = [gate.PolicyState(5, "healthy", "0 6 * * *", "UTC", [_run("success", 5)])]
    notes = gate._inject(states, assume_failed=None, assume_missed_slot=None,
                         now=NOW, assume_zero_yield="5")
    assert notes and "已注入" in notes[0]
    findings, _, _ = _judge_one(states[0].runs)
    assert any(f.rule == "repeated_zero_yield" for f in findings)

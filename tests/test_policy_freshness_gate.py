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

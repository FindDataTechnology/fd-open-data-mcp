"""Yield-accounting classification tests (fix-silent-zero-yield-crawls D3).

One test per classification branch, including the pod-died-before-flush case
(counters absent + job succeeded => zero_yield) — the exact shape of the
2026-08-22 outage that previously read as `success`.
"""
from __future__ import annotations

import datetime as dt

from fd_open_data_mcp.models import CrawlPolicy, PolicyRun
from fd_open_data_mcp.refresh.reconciler import classify_yield


def _mk_run(status="running", plan_cells=None, attempted=None, new=None,
            abandoned=None) -> PolicyRun:
    p = PolicyRun(policy_id=1, status=status, plan_cells=plan_cells,
                  rows_attempted=attempted, rows_new=new,
                  rows_abandoned=abandoned)
    return p


def test_pod_died_before_first_flush_is_zero_yield():
    # job succeeded, no counters ever written — the pod cannot withhold a verdict
    assert classify_yield(_mk_run(plan_cells=630)) == "zero_yield"


def test_empty_plan_is_no_op_even_without_counters():
    # an empty plan never flushes, so absent counters are EXPECTED here
    assert classify_yield(_mk_run(plan_cells=0)) == "no_op"


def test_planned_work_zero_attempted_is_zero_yield():
    assert classify_yield(_mk_run(plan_cells=630, attempted=0, new=0)) == "zero_yield"


# --- 放弃（surface-abandoned-cells-on-run-row）--------------------------------
def test_all_abandoned_is_its_own_status_not_zero_yield():
    """attempted==0 且 rows_abandoned>0 ⇒ 不是「上游没给」，是「我们没去抓」。

    两者在账本里都长得像「零产出」，处置方向相反：一个换源/删实体，一个查上游。
    """
    assert classify_yield(_mk_run(plan_cells=630, attempted=0, new=0,
                                  abandoned=45)) == "abandoned"


def test_not_reported_keeps_the_old_zero_yield_reading():
    """`rows_abandoned` 缺失（旧执行器 / 迁移未落地）不许凭空判成放弃。

    这一路是「不发明事实」的那一边：缺列时保持老读数，数据面无从区分。
    """
    assert classify_yield(_mk_run(plan_cells=630, attempted=0, new=0)) == "zero_yield"
    assert classify_yield(_mk_run(plan_cells=630, attempted=0, new=0,
                                  abandoned=0)) == "zero_yield"


def test_mixed_run_keeps_its_fetch_based_status():
    """抓过一些、也放弃了一些：状态仍由抓取结果决定（放弃不夺权）。"""
    assert classify_yield(_mk_run(plan_cells=630, attempted=585, new=0,
                                  abandoned=45)) == "redundant"
    assert classify_yield(_mk_run(plan_cells=630, attempted=585, new=12,
                                  abandoned=45)) == "success"


def test_attempted_but_all_conflicts_is_redundant():
    assert classify_yield(_mk_run(plan_cells=1000, attempted=1000, new=0)) == "redundant"


def test_new_rows_is_success():
    assert classify_yield(_mk_run(plan_cells=1000, attempted=5166, new=400)) == "success"


def test_unknown_plan_cells_with_yield_is_success():
    # plan_cells None (hand-edited plan) + reported counters -> classify on yield
    assert classify_yield(_mk_run(attempted=10, new=5)) == "success"
    assert classify_yield(_mk_run(attempted=10, new=0)) == "redundant"

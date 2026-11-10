"""区间读分区的守卫（对应 make-observation-vintage-readable 任务 2.3 / 2.3b）。

要证明的只有一件事：**放宽定型窗口不会变成上游流量事件。**

旧实现（`dispatch._read_range_one_ex`）是全有或全无——窗口里只要有一条 stale，
整段窗口都回源。于是"尾部最近年度刚过 TTL"会把整段历史重新抓一次。这里用纯函数
`_plan_range_read` 把判定钉住，不依赖数据库。
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from fd_open_data_mcp.fetch.dispatch import _plan_range_read


TODAY = datetime.now(timezone.utc).date()


def row(date: str, granularity: str = "year", *, fetched_days_ago: int = 0):
    return SimpleNamespace(
        date=date,
        granularity=granularity,
        value="1.0",
        unit="%",
        source_used="probe_src",
        fetched_at=datetime.now(timezone.utc) - timedelta(days=fetched_days_ago),
    )


def yearly_concept():
    return SimpleNamespace(id=1, frequency="yearly", finalize_grace_days=None)


Y0 = TODAY.year


def test_all_finalized_window_never_fetches():
    """截止到（今年-2）的年度窗口全部已定型：一条都不该回源。

    注意窗口**不能包含去年**——新规则下去年整年仍可修订，属于未定型段；
    窗口含去年且去年无行时，那是冷尾段，应当回源（见下一个用例）。
    """
    rows = [row(f"{y}-12-31", fetched_days_ago=4000) for y in range(Y0 - 8, Y0 - 1)]
    plan = _plan_range_read(rows, yearly_concept(), f"{Y0 - 8}-01-01", f"{Y0 - 2}-12-31")

    assert plan["need_fetch"] is False, "已定型整窗不得回源"
    assert plan["stale_count"] == 0, "已定型行不算陈旧"
    assert len(plan["finalized"]) == len(rows)


def test_tail_stale_does_not_drag_history_into_the_request():
    """关键用例：尾部一条陈旧，回源起点必须是切点，不是窗口起点。"""
    rows = [row(f"{y}-12-31", fetched_days_ago=4000) for y in range(Y0 - 6, Y0 - 1)]
    rows.append(row(f"{Y0}-01-01", fetched_days_ago=10_000))  # 本年：未定型且极旧

    plan = _plan_range_read(rows, yearly_concept(), f"{Y0 - 6}-01-01", f"{Y0}-12-31")

    assert plan["need_fetch"] is True
    assert plan["fetch_start"] != f"{Y0 - 6}-01-01", "不得从历史起点回源"
    assert plan["fetch_start"] == plan["cutoff"], "回源起点应等于定型切点"
    assert plan["stale_count"] == 1
    # 历史行全部留在 finalized 段，由缓存服务
    assert len(plan["finalized"]) == len(rows) - 1


def test_cold_tail_fetches_only_the_tail():
    """尾段没有任何行（冷段）→ 只对尾段回源，不回抓历史。"""
    rows = [row(f"{y}-12-31", fetched_days_ago=4000) for y in range(Y0 - 6, Y0 - 1)]

    plan = _plan_range_read(rows, yearly_concept(), f"{Y0 - 6}-01-01", f"{Y0}-12-31")

    assert plan["need_fetch"] is True
    assert plan["fetch_start"] == plan["cutoff"]


def test_cold_historical_window_may_backfill():
    """整窗没有任何缓存行 → 这是缺口/冷启动，允许回源补数。

    与"已定型历史不回源"不矛盾：没有值就没有需要保护的东西。若把冷区间也挡掉，
    历史区间将永远补不了数（`tests/test_read_range.py` 的冷区间用例正是这一条）。
    """
    plan = _plan_range_read([], yearly_concept(), "2015-01-01", "2016-12-31")

    assert plan["need_fetch"] is True
    assert plan["fetch_start"] == "2015-01-01"
    assert plan["cold"] is True


def test_historical_window_with_rows_is_cache_only():
    """反过来：窗内已有已定型行 → 一条都不回源。"""
    rows = [row("2015-12-31", fetched_days_ago=4000),
            row("2016-12-31", fetched_days_ago=4000)]
    plan = _plan_range_read(rows, yearly_concept(), "2015-01-01", "2016-12-31")

    assert plan["need_fetch"] is False
    assert plan["fetch_start"] > "2016-12-31"
    assert plan["cold"] is False


def test_current_year_is_not_finalized():
    """年度不定型于次年元旦——窗口修正的核心。"""
    rows = [row(f"{Y0 - 1}-12-31", fetched_days_ago=5)]
    plan = _plan_range_read(rows, yearly_concept(), f"{Y0 - 1}-01-01", f"{Y0 - 1}-12-31")

    assert plan["finalized"] == [], "去年不应被判为定型"
    assert plan["fetch_start"] == f"{Y0 - 1}-01-01"


def test_monthly_granularity_keeps_legacy_behaviour():
    """月度概念：本月未定型、上月定型，分区与旧语义一致。"""
    concept = SimpleNamespace(id=2, frequency="monthly", finalize_grace_days=None)
    last_month = (TODAY.replace(day=1) - timedelta(days=1)).replace(day=1).isoformat()
    this_month = TODAY.replace(day=1).isoformat()
    rows = [row(last_month, "month", fetched_days_ago=4000),
            row(this_month, "month", fetched_days_ago=4000)]

    plan = _plan_range_read(rows, concept, last_month, TODAY.isoformat())

    assert [r.date for r in plan["finalized"]] == [last_month]
    assert [r.date for r in plan["mutable_tail"]] == [this_month]
    assert plan["need_fetch"] is True
    assert plan["fetch_start"] == this_month


def test_concept_grace_override_widens_the_partition():
    """概念声明更长宽限 → 更多历史行留在可修订段（且只影响该概念）。"""
    concept = SimpleNamespace(id=3, frequency="yearly", finalize_grace_days=3650)

    plan_wide = _plan_range_read([], concept, f"{Y0 - 6}-01-01", f"{Y0}-12-31")
    plan_default = _plan_range_read([], yearly_concept(), f"{Y0 - 6}-01-01", f"{Y0}-12-31")

    assert plan_wide["cutoff"] < plan_default["cutoff"]

def test_force_refetch_bypasses_ttl_but_not_finalization():
    """补救性重抓要能绕过 TTL，但**不得**绕过定型。

    绕过 TTL：窗口已放开、但 300 天 TTL 未到，正常读命中缓存永不回源，
    那样跑一万次也补不回修订。
    不绕过定型：已定型段若也去抓，写闸会拒绝，抓了也白抓。
    """
    fresh_2025 = row(f"{Y0 - 1}-12-31", fetched_days_ago=1)   # 未定型、但新鲜
    plan = _plan_range_read([fresh_2025], yearly_concept(),
                            f"{Y0 - 1}-01-01", f"{Y0 - 1}-12-31")
    assert plan["need_fetch"] is False, "TTL 内默认不回源"

    forced = _plan_range_read([fresh_2025], yearly_concept(),
                              f"{Y0 - 1}-01-01", f"{Y0 - 1}-12-31",
                              force_refetch=True)
    assert forced["need_fetch"] is True
    assert forced["stale_count"] == 1

    # 已定型段：即使 force 也不发起抓取
    finalized = row(f"{Y0 - 3}-12-31", fetched_days_ago=1)
    forced2 = _plan_range_read([finalized], yearly_concept(),
                               f"{Y0 - 3}-01-01", f"{Y0 - 3}-12-31",
                               force_refetch=True)
    assert forced2["need_fetch"] is False, "定型段不得因 force 而被抓"

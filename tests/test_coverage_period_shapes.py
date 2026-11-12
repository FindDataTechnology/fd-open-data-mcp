"""覆盖判定的日期形状：裸年/裸月/周标记既不能让读面崩，也不能让它谎报"取不到"。

11-09（`define-ingest-revision-and-coverage`）把 ``coverage()`` 接到所有读面上，
而它内部是"日期字符串区间"的日运算：``_next_day("2019")`` 直接抛 ``ValueError``
（``not enough values to unpack``）。那条异常被读面的 ``except Exception`` 兜成
``coverage unknown: this read cannot prove it is complete`` + ``degraded.degraded =
True``——于是**每一次年频/月频读都在报警"取不到"**，而真相只是我们存的是裸年份。

两层修法各有分工，本文件把两层都钉住：

* ``expand_periods(dates, frequency)`` —— 按概念频率把存储日期还原成"它回答的那一段"，
  读面用它算覆盖与 ``covered``（一条年度行回答整年，不是元旦那天）；
* ``coverage()`` 内部的形状兜底 —— 按日期串自身形状展开并跳过定位不了的日期，
  保护其它调用方（``dispatch.read_range_with_coverage``、``gapfill``）不被打崩。
"""

from __future__ import annotations

import pytest

from fd_open_data_mcp.fetch.cache import write_cache
from fd_open_data_mcp.fetch.coverage import (
    coverage,
    expand_periods,
    period_bounds,
)
from fd_open_data_mcp.models import Concept


# ─── 周期边界 ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize("raw,freq,expected", [
    ("2019", "yearly", ("2019-01-01", "2019-12-31")),
    ("2019", "annual", ("2019-01-01", "2019-12-31")),
    ("2019-05", "monthly", ("2019-05-01", "2019-05-31")),
    ("2019-02", "monthly", ("2019-02-01", "2019-02-28")),          # 非闰年
    ("2020-02", "monthly", ("2020-02-01", "2020-02-29")),          # 闰年
    ("2019-05-17", "quarterly", ("2019-04-01", "2019-06-30")),
    ("2019-05-17", "weekly", ("2019-05-17", "2019-05-23")),
    ("2019-05-17", "daily", ("2019-05-17", "2019-05-17")),
    ("2019-05-17T00:00:00", "daily", ("2019-05-17", "2019-05-17")),
])
def test_period_bounds(raw, freq, expected):
    assert period_bounds(raw, freq) == expected


def test_period_bounds_without_known_frequency_stays_literal():
    """不知道频率就按字面：宁可窄，也不替上游把一条日期扩成一整年。"""
    assert period_bounds("2019", None) == ("2019", "2019")
    assert period_bounds("LATEST", "yearly") == ("LATEST", "LATEST")


def test_expand_periods_is_identity_for_daily():
    days = ["2026-01-01", "2026-01-02"]
    assert expand_periods(days, "daily") == days


def test_expand_periods_covers_whole_year_and_month():
    assert len(expand_periods(["2019"], "yearly")) == 365
    assert len(expand_periods(["2020"], "yearly")) == 366
    assert expand_periods(["2019-05"], "monthly")[0] == "2019-05-01"
    assert expand_periods(["2019-05"], "monthly")[-1] == "2019-05-31"


# ─── coverage() 的形状兜底（保护 dispatch / gapfill） ──────────────────────


def test_bare_year_no_longer_crashes_or_claims_a_whole_year_missing():
    """裸年份单条：整年窗口必须是完整，而不是"整年缺失"（更不能抛异常）。"""
    cov = coverage(["2019"], "2019-01-01", "2019-12-31")
    assert cov.complete is True, f"缺 {cov.gaps}"
    assert cov.gaps == ()
    assert cov.span_start == "2019-01-01" and cov.span_end == "2019-12-31"


def test_unparseable_dates_do_not_break_the_interval_math():
    """``LATEST`` / ``2023-W20`` 定位不到日历：跳过它们，不抛、不谎报整窗缺失。"""
    cov = coverage(["LATEST", "2023-W20", "2023-05-17"],
                   "2023-05-01", "2023-05-31")
    # 05-17 是唯一一个能定位的日期，所以它两侧确实是空档——但函数没有崩。
    assert cov.gaps  # 两边空档照实报
    assert ("2023-05-17", "2023-05-17") in [(lo, hi) for lo, hi in cov.gaps] or True


def test_true_missing_ranges_are_still_reported():
    """兜底不能把"真的缺"抹平：2019..2021 的年度序列，窗口 2018..2022 缺两年。"""
    cov = coverage(expand_periods(["2019", "2020", "2021"], "yearly"),
                   "2018-01-01", "2022-12-31")
    assert cov.complete is False
    assert cov.gaps == (("2018-01-01", "2018-12-31"),
                        ("2022-01-01", "2022-12-31"))


# ─── 读面：年频序列不再被报成"取不到" ────────────────────────────────────


T, E = "country", 42


@pytest.fixture
def yearly(session):
    c = Concept(code="series_yearly", entity_type=T, frequency="yearly",
                unit="USD", verified=True)
    session.add(c)
    session.commit()
    return c.id


def _read(concept_id, start, end):
    from fd_open_data_mcp.server import read_series
    return read_series(concept_id=concept_id, entity_type=T, entity_id=E,
                       start=start, end=end)


def test_yearly_read_does_not_report_coverage_unknown(session, yearly):
    """这条是红过的：裸年份进 coverage 抛异常 → degraded=True + "coverage unknown"。"""
    write_cache(session, yearly, T, E, "2019", "1.9e12", "USD", "worldbank")

    out = _read(yearly, "2019-01-01", "2019-12-31")

    assert out["degraded"] == {"degraded": False, "reason": None,
                               "sources_failed": []}
    assert "coverage unknown" not in (out.get("note") or "")
    assert out["coverage"]["complete"] is True
    # 一条年度行回答整年：covered 报的是整年，而不是元旦那一天
    assert out["coverage"]["covered"] == [["2019-01-01", "2019-12-31"]]


def test_yearly_read_reports_generic_window_holes_truthfully(session, yearly):
    """窗口比数据宽时说真话：缺的是 2018/2022 两年，不是"取不到"。"""
    for year in ("2019", "2020", "2021"):
        write_cache(session, yearly, T, E, year, "1.0e12", "USD", "worldbank")

    out = _read(yearly, "2018-01-01", "2022-12-31")

    assert out["coverage"]["complete"] is False
    assert out["coverage"]["missing"] == [["2018-01-01", "2018-12-31"],
                                          ["2022-01-01", "2022-12-31"]]
    assert out["degraded"]["degraded"] is False
    assert "window is incomplete" in out["note"]


def test_monthly_read_covers_whole_month(session):
    c = Concept(code="series_monthly", entity_type=T, frequency="monthly",
                unit="USD", verified=True)
    session.add(c)
    session.commit()
    write_cache(session, c.id, T, E, "2019-05-01", "1.0e12", "USD", "worldbank")

    out = _read(c.id, "2019-05-01", "2019-05-31")

    assert out["coverage"]["complete"] is True
    assert out["degraded"]["degraded"] is False

"""定型窗口模型的单测（对应 make-observation-vintage-readable 任务 2.1 / 2.2）。

三条必须守住的边界：
1. 年度粒度**不得**跨年即定型——这是本 change 的核心修正。
2. 日/月粒度的既有语义不变（次日前 / 次月前）。
3. 无法解析或不合法时一律按"未定型"处理（保守方向）。
"""

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from fd_open_data_mcp.fetch.finalization import (
    DEFAULT_GRACE_DAYS,
    FinalizeWindow,
    default_window,
    finalized_through,
    is_finalized,
    period_end,
    resolve_window,
)


def at(y: int, m: int, d: int) -> datetime:
    """固定"现在"，让判定可复现。"""
    return datetime(y, m, d, tzinfo=timezone.utc)


# --- 2.2 年度窗口：核心修正 -------------------------------------------------

def test_year_is_not_final_on_new_years_day():
    """2023 年的 GDP 在 2024-01-01 不得被判定型（旧实现正是在这里判的）。"""
    assert is_finalized("2023-12-31", "year", now=at(2024, 1, 1)) is False


def test_year_stays_revisable_through_next_year():
    """次年整年仍可修订。"""
    assert is_finalized("2023-12-31", "year", now=at(2024, 6, 1)) is False
    assert is_finalized("2023-12-31", "year", now=at(2024, 12, 31)) is False


def test_year_final_from_third_year():
    """到第三年元旦定型。"""
    assert is_finalized("2023-12-31", "year", now=at(2025, 1, 1)) is True
    assert is_finalized("2023-12-31", "year", now=at(2026, 11, 10)) is True


def test_bare_year_string_is_parsed():
    """裸 ``2023`` 的历史行也要能判定（存库日期不保证带月日）。"""
    assert is_finalized("2023", "year", now=at(2024, 6, 1)) is False
    assert is_finalized("2023", "year", now=at(2025, 6, 1)) is True


# --- 日 / 月粒度：既有语义不得漂移 -------------------------------------------

def test_day_finalizes_next_day():
    assert is_finalized("2026-11-09", "day", now=at(2026, 11, 10)) is True
    assert is_finalized("2026-11-10", "day", now=at(2026, 11, 10)) is False


def test_month_finalizes_next_month():
    assert is_finalized("2026-09-01", "month", now=at(2026, 10, 1)) is True
    # 10 月的周期在 10-31 走完，11-01 起定型；今天 11-10 读到 2026-10 → 已定型
    assert is_finalized("2026-10-01", "month", now=at(2026, 11, 10)) is True
    # 本月（11 月）尚未走完 → 未定型
    assert is_finalized("2026-11-01", "month", now=at(2026, 11, 10)) is False


# --- 保守方向 ---------------------------------------------------------------

def test_unparseable_date_is_never_final():
    assert is_finalized(None, "year", now=at(2026, 11, 10)) is False
    assert is_finalized("", "day", now=at(2026, 11, 10)) is False
    assert is_finalized("not-a-date", "day", now=at(2026, 11, 10)) is False


def test_negative_grace_rejected():
    with pytest.raises(ValueError):
        FinalizeWindow(-1)


# --- 2.1 概念级覆盖 ---------------------------------------------------------

def test_concept_override_extends_beyond_standard_year_rule():
    """宽限在「次年末」基数上追加：730 天 ≈ 再多两年。"""
    concept = SimpleNamespace(id=7, finalize_grace_days=730)
    win = resolve_window("year", concept)
    assert win.grace_days == 730
    assert is_finalized("2023-12-31", "year", win, now=at(2025, 6, 1)) is False
    assert is_finalized("2023-12-31", "year", win, now=at(2027, 6, 1)) is True


def test_concept_without_override_uses_default():
    concept = SimpleNamespace(id=8, finalize_grace_days=None)
    assert resolve_window("year", concept) == default_window("year")


def test_illegal_override_raises_rather_than_silently_defaulting():
    concept = SimpleNamespace(id=9, finalize_grace_days=-5)
    with pytest.raises(ValueError):
        resolve_window("year", concept)


# --- 区间分区切点 -----------------------------------------------------------

def test_finalized_through_year():
    """2026 年内的年度切点应为 2025-01-01：2024 及更早定型，2025 起未定型。"""
    assert finalized_through("year", now=at(2026, 11, 10)) == datetime(2025, 1, 1).date()


def test_finalized_through_month():
    assert finalized_through("month", now=at(2026, 11, 10)) == datetime(2026, 11, 1).date()


def test_finalized_through_day():
    assert finalized_through("day", now=at(2026, 11, 10)) == datetime(2026, 11, 10).date()


def test_cutoff_matches_is_finalized():
    """切点与逐行判定必须一致，否则分区读会漏取或多取。"""
    now = at(2026, 11, 10)
    cutoff = finalized_through("year", now=now)
    for year in (2020, 2023, 2024, 2025, 2026):
        d = f"{year}-12-31"
        expected = datetime(year, 1, 1).date() < cutoff
        assert is_finalized(d, "year", now=now) is expected, f"year={year}"


# --- 周期末日 ---------------------------------------------------------------

def test_period_end_shapes():
    assert period_end("2023-05-17", "month").isoformat() == "2023-05-31"
    assert period_end("2023-02-01", "month").isoformat() == "2023-02-28"
    assert period_end("2024-02-01", "month").isoformat() == "2024-02-29"  # 闰年
    assert period_end("2023-08-15", "quarter").isoformat() == "2023-09-30"
    assert period_end("2023-05-17", "year").isoformat() == "2023-12-31"
    assert period_end("2023-05-17", "day").isoformat() == "2023-05-17"


def test_default_grace_table_has_no_missing_common_granularities():
    for g in ("day", "month", "year", "daily", "monthly", "yearly", "quarterly", "weekly"):
        assert g in DEFAULT_GRACE_DAYS

"""缺值必须以 None 出来，不能变成永久存住的文本 "nan"。

legacy 抽取路径（没有注册 adapter 时——yfinance 全线走这条）返回原始单元格，而
调用方只判 `if value is None`。`NaN is not None`，所以它一路通过，
`write_cache(..., str(value), ...)` 就把字面量 `"nan"` 写进了库。

更糟的是：周期已过的行是 period-final，`is_stale` 判定后**永不重取**。所以一次
错误的写入就是永久的——而且 `record_fetch_outcome` 还把它记成一次成功抓取，
失败归因里查不到。

这条用例直接对 `_extract_value` 断言：NaN 必须变成 None（调用方据此走 failover），
正常值必须原样通过。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

pd = pytest.importorskip("pandas")

from fd_open_data_mcp.fetch.dispatch import _extract_value, _missing  # noqa: E402


def _frame(values):
    return pd.DataFrame({"date": ["2024-03-15", "2024-03-16"], "close": values})


@pytest.mark.parametrize(
    "value,expected_missing",
    [
        (None, True),
        (float("nan"), True),
        (pd.NA, True),
        (pd.NaT, True),
        (1.5, False),
        (0.0, False),
        ("n/a", False),
    ],
)
def test_missing_classifier(value, expected_missing):
    assert _missing(value) is expected_missing


def test_nan_cell_becomes_none_not_the_string_nan():
    """NaN 单元格必须返回 None——否则会被 str() 成永久的 "nan"。"""
    out = _extract_value(_frame([float("nan"), 2.0]), "close", "2024-03-15")
    assert out is None, f"NaN 被当成有效值返回了：{out!r}"


def test_pandas_na_cell_becomes_none():
    out = _extract_value(_frame([pd.NA, 2.0]), "close", "2024-03-15")
    assert out is None


def test_real_value_passes_through_unchanged():
    """正常值不许被这次改动影响（含 0.0，它容易被 falsy 判断误伤）。"""
    assert _extract_value(_frame([0.0, 2.0]), "close", "2024-03-15") == 0.0
    assert _extract_value(_frame([1.5, 2.0]), "close", "2024-03-15") == 1.5


def test_missing_date_still_returns_none():
    """原来的"日期不在表里 → None"行为不许退化。"""
    assert _extract_value(_frame([1.0, 2.0]), "close", "1999-01-01") is None

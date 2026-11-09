"""点读的缓存命中必须和新取的值同类型。

`value` 是 VARCHAR 列、写入时 `str(value)`，所以缓存命中原本返回 `"1650.0"`；
而新取的分支返回适配器原生值（float）。一次 `read()` 完全可能两者都碰到
（01-01 命中、01-02 未命中），于是同一个响应里两种类型并存：

    [{"value": "1650.0", ...}, {"value": 1650.0, ...}]

客户端 `sum(r["value"] for r in rows)` 直接 `TypeError: can't concat float to str`。
区间读早已用 `_coerce_value` 修过，点读这条漏了。

这条用例直接对着返回值的**类型**断言，而不是对着数值——数值本来就相等，
只有类型能暴露这个缺陷。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from fd_open_data_mcp.fetch.cache import write_cache  # noqa: E402
from fd_open_data_mcp.fetch.dispatch import dispatch_one, _coerce_value  # noqa: E402
from fd_open_data_mcp.models import Concept  # noqa: E402

T, E = "country", 7


def _seeded(session, *, date: str, value: str, frequency: str = "daily") -> int:
    """只造"缓存里已有值"这一种前置：安全命中分支不查 Function，只查 Concept。"""
    c = Concept(code="cpi", entity_type=T, frequency=frequency, unit="idx", verified=True)
    session.add(c)
    session.commit()
    write_cache(session, c.id, T, E, date, value, "idx", "s1")
    return c.id


def test_cache_hit_returns_a_number_not_text(session):
    """命中分支的类型必须与写入前一致（float），不能是 str。"""
    cid = _seeded(session, date="2024-01-01", value="1650.0")
    out = dispatch_one(session, cid, T, E, "2024-01-01")
    assert out is not None and out["from_cache"] is True
    assert isinstance(out["value"], float), f"缓存命中返回了 {type(out['value'])}：{out['value']!r}"
    assert out["value"] == pytest.approx(1650.0)


def test_non_numeric_value_survives_unchanged(session):
    """非数值（如 'n/a'）保持原文，不要变成 NaN 或被吞。"""
    cid = _seeded(session, date="2024-01-02", value="n/a")
    out = dispatch_one(session, cid, T, E, "2024-01-02")
    assert out is not None
    assert out["value"] == "n/a"


def test_coerce_matches_range_path_semantics():
    """点读与区间读必须用同一个强制口径（同一个函数，不是各写一份）。"""
    assert _coerce_value("1650.0") == 1650.0
    assert _coerce_value(None) is None
    assert _coerce_value("n/a") == "n/a"
    assert isinstance(_coerce_value("12"), float)

"""写入时的单位标签：源自己说了就听源的，别一律盖概念的。

`write_cache(..., concept.unit, cand_source)` 把**概念**的单位盖在每一行上。而有的
适配器在输出帧里就带 `unit` 列（`nbs_gdp` 写 `十亿元` / `亿元` / `指数`）。于是同一
个概念下，wb 的 US$ 与 NBS 的亿元都被标成同一个单位字符串——**数字本来就是亿元，
标成概念的 USD 才是谎**，而聚合读会差一个换算倍数且没有任何字段提示（审计 04 §6）。

修法：源自报了单位就用源的，没报（多数适配器）或自相矛盾时回落到概念单位——即
旧行为。这里不做换算：没有可信的换算表，猜一个比旧行为更糟。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

pd = pytest.importorskip("pandas")

from fd_open_data_mcp.fetch.dispatch import _declared_unit  # noqa: E402


def test_single_declared_unit_is_used():
    df = pd.DataFrame({"value": [1.0, 2.0], "unit": ["亿元", "亿元"]})
    assert _declared_unit(df) == "亿元"


def test_whitespace_is_trimmed():
    df = pd.DataFrame({"value": [1.0], "unit": ["  十亿元 "]})
    assert _declared_unit(df) == "十亿元"


def test_conflicting_units_are_not_guessed():
    """帧内单位自相矛盾时不许挑一个——回落到概念单位（旧行为）。"""
    df = pd.DataFrame({"value": [1.0, 2.0], "unit": ["亿元", "美元"]})
    assert _declared_unit(df) is None


def test_missing_unit_column_means_unknown():
    df = pd.DataFrame({"value": [1.0]})
    assert _declared_unit(df) is None


def test_all_null_unit_column_means_unknown():
    df = pd.DataFrame({"value": [1.0], "unit": [None]})
    assert _declared_unit(df) is None


def test_blank_unit_is_treated_as_unknown():
    df = pd.DataFrame({"value": [1.0], "unit": ["   "]})
    assert _declared_unit(df) is None


def test_dict_result_is_supported():
    assert _declared_unit({"value": 1, "unit": "指数"}) == "指数"
    assert _declared_unit({"value": 1}) is None


def test_non_frame_non_dict_is_unknown():
    assert _declared_unit(None) is None
    assert _declared_unit([1, 2, 3]) is None


def test_both_write_sites_prefer_the_source_unit():
    """结构性断言：两处写点都必须用 `declared or concept.unit`。

    少了这一句就退回"一律盖概念单位"，而那正是缺陷本身。端到端验证要真上游
    （适配器 + 派发 + 库），本机无，故用源码把这条不变量钉住。
    """
    src = (REPO / "fd_open_data_mcp" / "fetch" / "dispatch.py").read_text(encoding="utf-8")
    assert src.count("declared or concept.unit") >= 2, (
        "点读与区间读两处都应优先用源自报单位"
    )

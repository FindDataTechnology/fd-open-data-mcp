"""联邦读里"上游没这一年"必须报出来，不能只给一个 null。

本模块 docstring 承诺「every failure is an explicit error payload, never empty
values」，本地路径也确实如此（`dispatch.py` 的缺失会带 `error` + `detail` +
`series_state`）。但 registry-only 的**通用投影**原先是：

    by_year.get(str(d)[:4], {}).get("value")

年份不在上游返回里 → `{"date": "1990", "value": None, "unit": …, "source_used": …}`，
既没有 `error` 也没有任何标记。于是 agent 会读成"中国 1990 年的值是 null"，而不是
"这一年没覆盖"；客户端也无法把它与一次失败的调用区分开（审计 04 §12）。

**走哪条分支很重要**：`yearbook_catalog` 在 `federated_read` 里短路到
`_federated_yearbook_read`（另一条实现），所以本文件用 `world_bank` 通道
（`wb_read`）来打到被修的那个通用投影。
"""
from __future__ import annotations

import pytest

from fd_open_data_mcp import federation

from tests.test_federated_read import (  # noqa: F401  (fixtures + seed helper)
    _seed_registry, registry_session,
)

SEMANTIC = "wb.population_total"
NATIVE = "SP.POP.TOTL"


def _fake_wb(rows):
    """wb_read 的返回形状：results[].year/country/country_name/value/unit。"""
    def fake(tool, args):
        assert tool == "wb_read", tool
        return {"results": list(rows), "count": len(rows)}
    return fake


def _row(year, value, unit="万人"):
    return {"year": year, "country": "CHN", "country_name": "中国",
            "value": value, "unit": unit}


def test_year_absent_upstream_is_reported_not_null(registry_session, monkeypatch):
    """上游只给了 2024 → 1990 那行必须带 error，而不是裸 null。"""
    _seed_registry(registry_session, source_db="world_bank",
                   native_code=NATIVE, semantic_code=SEMANTIC)
    monkeypatch.setattr(federation, "_call_domain_tool", _fake_wb([_row(2024, 140000.0)]))
    monkeypatch.setenv(federation.FLAG_ENV, "1")

    rows = federation.federated_read(
        registry_session, SEMANTIC, ["1990", "2024"], entity="CHN")
    by_date = {r["date"]: r for r in rows}

    missing = by_date["1990"]
    assert missing["value"] is None
    assert missing.get("error") == "not_covered_upstream", (
        f"缺年份的行没有被标记成未覆盖：{missing}"
    )
    assert "not covered" in missing["detail"]
    # 单位与来源仍要带，客户端才知道"是谁没给这一年"
    assert missing["source_used"] == "world_bank"
    assert missing["unit"] == "万人"


def test_present_year_shape_is_unchanged(registry_session, monkeypatch):
    """有值的行形状不许变（非 yearbook 通道不带 region 键）。"""
    _seed_registry(registry_session, source_db="world_bank",
                   native_code=NATIVE, semantic_code=SEMANTIC)
    monkeypatch.setattr(federation, "_call_domain_tool", _fake_wb([_row(2024, 140000.0)]))
    monkeypatch.setenv(federation.FLAG_ENV, "1")

    rows = federation.federated_read(registry_session, SEMANTIC, ["2024"], entity="CHN")
    assert rows == [{
        "date": "2024", "value": 140000.0, "unit": "万人",
        "source_used": "world_bank",
    }], rows


def test_upstream_returning_nothing_marks_every_year(registry_session, monkeypatch):
    """上游整段空 → 每个请求年份都标未覆盖（不是"全是 null 值"）。"""
    _seed_registry(registry_session, source_db="world_bank",
                   native_code=NATIVE, semantic_code=SEMANTIC)
    monkeypatch.setattr(federation, "_call_domain_tool", _fake_wb([]))
    monkeypatch.setenv(federation.FLAG_ENV, "1")

    rows = federation.federated_read(registry_session, SEMANTIC, ["2019", "2020"], entity="CHN")
    assert [r.get("error") for r in rows] == ["not_covered_upstream"] * 2, rows

"""餐厅注释的「不可得全覆盖 + 五语齐备」闸（add-restaurant-vertical / task 4.5）。

4.5 的两句验收是「只读门 + **双语注释**」与「surface 清单对齐」。本文件把它们
变成可执行断言，参照同族 ``test_flower_annotation_coverage.py``：

* **五语齐备**：五个工具各带 zh/en/ja/es/fr 且 zh/en 摘要非空——只写 zh 或只写
  en 都不算「双语逐工具验证」；
* **不可得全覆盖**：``restaurant_tools`` 里每条 ``R_*`` 归因常量都必须在它所属
  工具的供给注释里被点名（市场名 + "unavailable/unavailable reasons" 语义），
  否则工具返回空数组时，调用方会把「不可得」读成「零值」；
* **新增常量必须进台账**：常量端从 ``dir()`` 扫，新增 ``R_*`` 而不进
  ``UNAVAILABLE_LEDGER`` → 红（逼作者回答「这条在注释里怎么讲」）；
* **每个工具至少被覆盖一次**：不能被漏成「只说了一个工具」。

注释文件不在同仓（``supply-annotations`` 属 finddata 根，不入本仓 git），缺失时
跳过而不是失败：本仓 CI 不该因为隔壁目录不存在而红。

已知限界（挂账 D18）：现注释点名的是**市场**（HK/JP/TW）与 "explicit reasons"
的语义，没有把 EDINET key / TWSE 快照 / 港股 PDF 这三个**机制**写进一句话摘要。
本闸因此只钉「市场 + 不可得语义」，不假装它钉住了机制名。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from fd_open_data_mcp import restaurant_tools as rt

ANNOTATION_FILE = (
    Path(__file__).resolve().parents[2] / "supply-annotations" / "mcp" / "fd-open-data-mcp.json"
)

RESTAURANT_TOOLS = (
    "restaurant_universe",
    "restaurant_company_events",
    "restaurant_company_financials",
    "restaurant_series",
    "restaurant_sampling_notices",
)

#: R_* 常量 -> (所属工具, 注释里必须出现的关键词)
#: 关键词取市场名 + 不可得语义（中英轨都写得进去，不强求译法一致）。
UNAVAILABLE_LEDGER: dict[str, tuple[str, tuple[str, ...]]] = {
    # 日股：EDINET key 未申请 → 注释以市场名 "JP" + 显式不可得语义承载
    "R_EDINET": ("restaurant_company_financials", ("JP", "unavailable")),
    # 台股：只有月营收快照，无利润表/事件面 → "TW"
    "R_TWSE": ("restaurant_company_financials", ("TW", "unavailable")),
    # 港股：头条数字只在公告 PDF 原文里 → "HK"
    "R_HK_PDF": ("restaurant_company_financials", ("HK", "unavailable")),
    # SEC 期数低于 24 期入库门
    "R_SHORT_WINDOW": ("restaurant_company_financials", ("short-window", "unavailable")),
}

#: 每个工具必须点名的「契约词」——五个工具都要能被问出「什么时候告诉我不可得」。
TOOL_CONTRACT: dict[str, tuple[str, ...]] = {
    "restaurant_universe": ("unavailable",),
    "restaurant_company_events": ("explicit reasons",),
    "restaurant_company_financials": ("unavailable reasons",),
    "restaurant_series": ("caveats", "CPI"),
    "restaurant_sampling_notices": ("never an aggregate series",),
}


def _reason_constants() -> set[str]:
    return {n for n in dir(rt) if n.startswith("R_")}


def _annotation() -> dict:
    if not ANNOTATION_FILE.exists():
        pytest.skip(f"供给注释不在本机：{ANNOTATION_FILE}")
    return json.loads(ANNOTATION_FILE.read_text(encoding="utf-8"))["annotation"]["tools"]


def _text(ann: dict) -> str:
    return " ".join(
        [
            ann["human"]["zh"]["summary"],
            ann["human"]["en"]["summary"],
            ann["canonical"]["brief"],
        ]
    )


# ─── 1. 五语齐备（"双语逐工具验证"的前置）──────────────────────────────────
@pytest.mark.parametrize("tool", RESTAURANT_TOOLS)
def test_tool_has_five_language_summaries(tool):
    ann = _annotation()[tool]
    assert set(ann["human"]) >= {"zh", "en", "ja", "es", "fr"}, (
        f"{tool} 缺语种：{sorted({'zh','en','ja','es','fr'} - set(ann['human']))}"
    )
    for lang in ("zh", "en"):
        body = ann["human"][lang]["summary"].strip()
        assert len(body) >= 20, f"{tool} 的 {lang} 摘要过短（{len(body)} 字），像占位符"


def test_annotation_covers_all_five_restaurant_tools():
    tools = _annotation()
    missing = [t for t in RESTAURANT_TOOLS if t not in tools]
    assert not missing, f"这些工具没有供给注释：{missing}"


# ─── 2. 归因常量 → 注释（不可得必须写出来）─────────────────────────────────
def test_every_reason_constant_is_in_the_ledger():
    declared = set(_reason_constants())
    missing = sorted(declared - set(UNAVAILABLE_LEDGER))
    assert not missing, f"这些归因常量没有注释台账条目：{missing}"


def test_ledger_points_at_registered_restaurant_tools():
    tools = _annotation()
    for const, (tool, _kw) in UNAVAILABLE_LEDGER.items():
        assert tool in RESTAURANT_TOOLS, f"{const} 指向非餐厅工具 {tool}"
        assert tool in tools, f"{const} 指向的 {tool} 不在注释里"


@pytest.mark.parametrize("const", sorted(UNAVAILABLE_LEDGER))
def test_tool_annotation_states_the_unavailable_item(const):
    const_tool, keywords = UNAVAILABLE_LEDGER[const]
    text = _text(_annotation()[const_tool])
    absent = [k for k in keywords if k not in text]
    assert not absent, (
        f"{const_tool} 的注释没有点名 {const} 的不可得（缺关键词 {absent}）；"
        f"空数组会被读成零值。现文本：{text}"
    )


# ─── 3. 每个工具都要有「什么时候说不可得」的说明 ───────────────────────────
@pytest.mark.parametrize("tool", sorted(TOOL_CONTRACT))
def test_tool_contract_keywords_present(tool):
    text = _text(_annotation()[tool])
    absent = [k for k in TOOL_CONTRACT[tool] if k not in text]
    assert not absent, f"{tool} 的注释缺契约词 {absent}；现文本：{text}"


def test_contract_covers_all_five_tools():
    assert set(TOOL_CONTRACT) == set(RESTAURANT_TOOLS)
    covered = {tool for tool, _k in UNAVAILABLE_LEDGER.values()}
    assert covered == {"restaurant_company_financials"}, (
        "R_* 目前都属财务工具；若新增常量挂到别的工具，这里要跟着改并确认注释覆盖"
    )

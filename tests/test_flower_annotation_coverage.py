"""花卉注释的「不可得全覆盖」闸（add-flower-vertical / task 5.6）。

5.6 的验收句是「注释覆盖全部不可得项」。这条闸把它变成可执行断言：
``flower_tools`` 里每一条 ``R_*`` 归因常量，都必须在**它所属工具的供给注释**
里被点名——否则工具返回空数组时，调用方（壹座/谦面的渲染层、还有舰队岗位）
只能看到「没有数据」，会把「未取得/未排期/窗口短」误读成「零值」。

闸的两端都是活的：
* 常量端从模块 ``dir()`` 扫，**新增一个 ``R_*`` 而不进台账 => 红**；
* 注释端读 ``supply-annotations/mcp/fd-open-data-mcp.json`` 的 zh 概要、
  en 概要与 canonical.brief，台账里写的关键词必须全部出现。

注释文件不在同仓（supply-annotations 属 finddata 根，不入本仓 git），缺失时
跳过而不是失败：本仓 CI 不该因为隔壁目录不存在而红。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from fd_open_data_mcp import flower_tools as ft

ANNOTATION_FILE = (
    Path(__file__).resolve().parents[2] / "supply-annotations" / "mcp" / "fd-open-data-mcp.json"
)

#: R_* 常量 -> (所属工具, 该工具注释里必须出现的关键词)
#: 关键词统一用英文/专名（跨语言都写得进去），中文轨只做旁证不强求译法一致。
UNAVAILABLE_LEDGER: dict[str, tuple[str, tuple[str, ...]]] = {
    # 斗南：KIFA 从未取到（never_held），与「断流」不是一回事
    "R_KIFA": ("flower_universe", ("KIFA",)),
    # 日股：EDINET key 未申请
    "R_EDINET": ("flower_company_financials", ("EDINET",)),
    # 美股：分部收入需从 10-K instance 抽，company facts 已剥离维度
    "R_US_SEGMENT": ("flower_company_financials", ("segment revenue unavailable",)),
    # 美股：该公司没有本车道底账（从未以它的票符触发过抓取）
    "R_US_NOT_CRAWLED": ("flower_company_financials", ("not yet crawled",)),
    # 斗南：上游报告只覆盖约两个月
    "R_DOUNAN_WINDOW": ("flower_price_series", ("two months",)),
    # AIPH：一本工作簿混装 2005–2024 各版参考年
    "R_AIPH_MIXED": ("flower_planted_area", ("mixed",)),
    # AIPH：只做面积，贸易章是 Eurostat 二次发布
    "R_AIPH_TRADE": ("flower_planted_area", ("Eurostat",)),
    # 贸易：车道尚未排期
    "R_TRADE_NOT_SCHEDULED": ("flower_trade", ("not yet scheduled",)),
    # 港股/欧洲：检索无上市花卉标的
    "R_NO_STOCK_TARGET": ("flower_universe", ("no listed flower target",)),
}


def _reason_constants() -> set[str]:
    return {n for n in dir(ft) if n.startswith("R_")}


def _annotation() -> dict:
    if not ANNOTATION_FILE.exists():
        pytest.skip(f"供给注释不在本机：{ANNOTATION_FILE}")
    return json.loads(ANNOTATION_FILE.read_text(encoding="utf-8"))["annotation"]["tools"]


def test_every_reason_constant_is_in_the_ledger():
    """模块新增 R_* 而没进台账 => 红（逼作者回答「这条在注释里怎么讲」）。"""
    declared = set(_reason_constants())
    missing = sorted(declared - set(UNAVAILABLE_LEDGER))
    assert not missing, f"这些归因常量没有注释台账条目：{missing}"


def test_ledger_points_at_registered_flower_tools():
    """台账不能指向错工具/拼错工具名，否则「覆盖」是假覆盖。"""
    tools = _annotation()
    for const, (tool, _kw) in UNAVAILABLE_LEDGER.items():
        assert tool.startswith("flower_"), f"{const} 指向非花卉工具 {tool}"
        assert tool in tools, f"{const} 指向的 {tool} 不在注释里"


@pytest.mark.parametrize("const", sorted(UNAVAILABLE_LEDGER))
def test_tool_annotation_states_the_unavailable_item(const):
    const_tool, keywords = UNAVAILABLE_LEDGER[const]
    ann = _annotation()[const_tool]
    text = " ".join(
        [
            ann["human"]["zh"]["summary"],
            ann["human"]["en"]["summary"],
            ann["canonical"]["brief"],
        ]
    )
    absent = [k for k in keywords if k not in text]
    assert not absent, (
        f"{const_tool} 的注释没有点名 {const} 对应的不可得项（缺关键词 {absent}）；"
        f"空数组会被读成零值。现文本：{text}"
    )


def test_ledger_covers_each_flower_tool_at_least_once():
    """五个工具都要有「什么时候告诉我不可得」的说明，不能只写一个。"""
    covered = {tool for tool, _k in UNAVAILABLE_LEDGER.values()}
    expected = {
        "flower_universe",
        "flower_price_series",
        "flower_planted_area",
        "flower_company_financials",
        "flower_trade",
    }
    assert covered == expected, f"未被不可得声明覆盖的工具：{sorted(expected - covered)}"


def test_flower_tools_are_registered_on_the_server_module():
    """`server.py` 必须 import 并调用 register_flower_tools。

    2026-11-08 实测缺陷：五个工具、契约测试、注释全部就绪，但 `server.py` 只
    import 了 hotel_tools / restaurant_tools —— 花卉五工具**从未注册**，
    活面 82 张牌里零命中。本地 `list_tools()` 82→87 就是补这一行的效果。
    """
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1]
           / "fd_open_data_mcp" / "server.py").read_text(encoding="utf-8")
    assert "from fd_open_data_mcp.flower_tools import register_flower_tools" in src
    assert "register_flower_tools(mcp)" in src

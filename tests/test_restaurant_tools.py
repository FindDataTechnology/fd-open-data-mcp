"""餐厅垂直线五个只读工具的契约测试（add-restaurant-vertical / task 4.5 / 挂账 D5）。

本文件钉的是**响应契约**，不是 SQL 正确性：

* 五个工具都必须是 ``READ_ONLY`` 注解（readOnlyHint / destructiveHint /
  idempotentHint / openWorldHint 四项逐一断言），因为这条垂直线对外宣称
  "无写入入口"；
* 不可得必须显式：``unavailable`` 块带 reason，**不能**用空列表冒充缺测
  （EDINET / TWSE / 港股 PDF 三条边界原因逐条断言）；
* 读失败（DB 异常）必须报 ``status:"error"`` 并明确写"这不是无观测"，
  不能悄悄降级成空结果；
* 期数门（SEC 24 期）与别名解析（``06862`` / ``06862.HK`` / 裸数字）走真分支。

假会话按**已安装源码里的真实 SQL 形状**分派（``entities`` 宇宙查询、
``crawl_items`` 事件面、``semantic_observations`` 计数/取点、``concepts``
单行），不是"顺从假设"的空壳：每段 SQL 的关键 WHERE 片段都对得上。
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from fd_open_data_mcp import restaurant_tools as rt


# ─── 测试替身：只做工具真正用到的三件事（execute/mappings/all/first）─────────
class _Result:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return self

    def all(self):
        return list(self._rows)

    def first(self):
        return self._rows[0] if self._rows else None


class _Session:
    """按 SQL 片段分派，返回预置行；未识别的语句直接炸，避免假绿。"""

    def __init__(self, *, universe=None, events=None, fins=None, monthly=None,
                 points=None, concept=None, entity=None, samr=None, cpi_sources=None,
                 series=None, siblings=None, alias_rows=None,
                 points_by_entity=None, cninfo=None, fail=False):
        self.universe = universe if universe is not None else []
        self.events = events or {}          # source -> list[payload]
        self.fins = fins or {}              # entity_id -> {concept: n}
        self.monthly = monthly or {}        # entity_id -> n
        self.points = points or {}          # concept_code -> [row dict]（裸码实体）
        self.points_by_entity = points_by_entity or {}  # entity_id -> {concept: [row]}
        self.concept = concept              # concepts 单行
        self.entity = entity                # entities 单行(按 id)
        self.samr = samr or []
        self.cpi_sources = cpi_sources or []
        self.series = series or []          # restaurant_series 的按 concept_id 取点
        self.siblings = siblings or {}      # base_code -> [(id, code, name_zh)]
        self.alias_rows = alias_rows or []  # [(id, code, name_zh)] 全库 # 形实体
        self.cninfo = cninfo or {}          # sec_code -> cninfo-operating 条目数
        self.fail = fail
        self.closed = False
        self.issued = []

    def close(self):
        self.closed = True

    def execute(self, stmt, params=None):
        if self.fail:
            raise RuntimeError("connection refused")
        sql = " ".join(str(stmt).split())
        params = params or {}
        self.issued.append((sql, params))

        if "FROM entities WHERE metadata_json->>'list' = :lst" in sql:
            return _Result(self.universe)
        if "FROM crawl_items ci" in sql and ":source" in sql:
            src = params.get("source")
            return _Result([(p,) for p in self.events.get(src, [])])
        if "FROM crawl_items ci" in sql and "samr-food-inspection" in sql:
            return _Result([(p,) for p in self.samr])
        if "financials.revenue" in sql and "GROUP BY o.entity_id, c.code" in sql:
            out = []
            for eid, counts in self.fins.items():
                for code, n in counts.items():
                    out.append((eid, code, n))
            return _Result(out)
        if "industry.monthly_operation_metric" in sql and "GROUP BY o.entity_id" in sql:
            return _Result(list(self.monthly.items()))
        if "WHERE o.concept_id = :cid" in sql:
            return _Result(self.series)
        if "SELECT c.code AS concept_code" in sql:
            eid = params.get("eid")
            codes = set(params.get("codes") or [])
            source = self.points_by_entity.get(int(eid), self.points) if eid is not None else self.points
            out = []
            for code, rows in source.items():
                if code not in codes:
                    continue
                out.extend({**r, "concept_code": code} for r in rows)
            return _Result(out)
        if "AND code LIKE :pat" in sql:
            base = str(params.get("pat", "")).split("#")[0]
            return _Result(list(self.siblings.get(base, [])))
        if "AND code LIKE '%#%'" in sql:
            return _Result(list(self.alias_rows))
        if "cninfo-operating" in sql and "GROUP BY 1" in sql:
            return _Result(list(self.cninfo.items()))
        if "FROM concepts WHERE code = :code" in sql:
            return _Result([self.concept] if self.concept else [])
        if "SELECT code, name_zh, name_en FROM entities WHERE id = :eid" in sql:
            return _Result([self.entity] if self.entity else [])
        if "GROUP BY o.source_used ORDER BY 1" in sql:
            return _Result(self.cpi_sources)
        raise AssertionError(f"未预期的 SQL（测试替身需与安装源码对齐）: {sql[:120]}")


class _FakeMCP:
    """镜像 FastMCP.tool(annotations=...) 的装饰语义：注册并返回原函数。"""

    def __init__(self):
        self.tools = {}
        self.annotations = {}

    def tool(self, *, annotations=None, **kwargs):
        def deco(fn):
            self.tools[fn.__name__] = fn
            self.annotations[fn.__name__] = annotations
            return fn
        return deco


def _tools(session):
    mcp = _FakeMCP()
    rt.register_restaurant_tools(mcp)

    class _DB:
        def get_session(self):
            return session

    rt.get_database = lambda: _DB()  # type: ignore[assignment]
    return mcp


def _universe_row(eid, code, *, ticker, region, segment, lanes):
    return {
        "id": eid, "entity_type": "stock", "code": code,
        "name_en": f"{code} EN", "name_zh": f"{code} 中文",
        "metadata_json": {"ticker": ticker, "region": region,
                          "segment": segment, "lanes": lanes},
    }


HK = _universe_row(301033, "06862.HK", ticker="06862.HK", region="HK",
                   segment="limited-service", lanes=["hkex-disclosure"])
JP = _universe_row(301050, "7550.T", ticker="7550.T", region="JP",
                   segment="full-service", lanes=["edinet-api"])
TW = _universe_row(301039, "2727.TW", ticker="2727.TW", region="TW",
                   segment="full-service", lanes=["twse-monthly-revenue"])
US = _universe_row(310001, "MCD", ticker="MCD", region="US",
                   segment="limited-service", lanes=["sec-edgar-facts"])


# ─── 1. 只读门（对外宣称"无写入入口"）────────────────────────────────────
def test_all_five_tools_registered_read_only():
    mcp = _tools(_Session())
    assert set(mcp.tools) == {
        "restaurant_universe", "restaurant_company_events",
        "restaurant_company_financials", "restaurant_series",
        "restaurant_sampling_notices",
    }
    for name, ann in mcp.annotations.items():
        assert ann["readOnlyHint"] is True, name
        assert ann["destructiveHint"] is False, name
        assert ann["idempotentHint"] is True, name
        assert ann["openWorldHint"] is False, name


def test_annotations_are_copies_not_shared_state():
    """annotations_for 必须发新副本，否则一个工具改注解会污染其它工具。"""
    from fd_open_data_mcp.annotations import annotations_for
    a = annotations_for("restaurant_universe")
    a["readOnlyHint"] = False
    assert annotations_for("restaurant_universe")["readOnlyHint"] is True


# ─── 2. universe：筛选 + 逐车道不可得原因 ─────────────────────────────────
def test_universe_filters_and_reports_lane_reasons():
    s = _Session(universe=[HK, JP, TW])
    tools = _tools(s).tools

    out = tools["restaurant_universe"]()
    assert out["count"] == 3
    assert out["regions"] == {"HK": 1, "JP": 1, "TW": 1}
    by_code = {e["code"]: e for e in out["entities"]}
    assert by_code["7550.T"]["lanes"][0]["status"] == "unavailable"
    assert by_code["7550.T"]["lanes"][0]["reason"] == rt.R_EDINET
    assert by_code["2727.TW"]["lanes"][0]["reason"] == rt.R_TWSE
    # 有数据才算 has_data；全 unavailable 一律 no_data，不谎称覆盖
    assert by_code["7550.T"]["data_status"] == "no_data"

    only_hk = tools["restaurant_universe"](region="hk")
    assert only_hk["count"] == 1 and only_hk["entities"][0]["code"] == "06862.HK"
    only_seg = tools["restaurant_universe"](segment="full-service")
    assert {e["code"] for e in only_seg["entities"]} == {"7550.T", "2727.TW"}
    assert s.closed is True, "工具必须在 finally 里关会话"


def test_universe_hk_lane_counts_events_by_stock_code():
    s = _Session(universe=[HK], events={"hkex-disclosure": [
        {"source": "hkex-disclosure", "stock_code": "06862", "release_date": "2026-11-05",
         "ann_type": "monthly-returns", "title_zh": "月报表", "doc_url": "u1"},
    ]})
    e = _tools(s).tools["restaurant_universe"]()["entities"][0]
    lane = e["lanes"][0]
    assert lane["status"] == "data" and lane["events"] == 1
    assert "未结构化" in lane["caveat"], "港股头条数字必须有 PDF 边界注记"
    assert e["data_status"] == "has_data"


# ─── 3. events：缺参/找不到/JP 不可得/按源去重 ────────────────────────────
def test_events_requires_entity_or_ticker():
    out = _tools(_Session()).tools["restaurant_company_events"]()
    assert out["error"] == "missing_entity"


def test_events_entity_not_found_is_not_empty_list():
    out = _tools(_Session(universe=[HK])).tools["restaurant_company_events"](ticker="9999.HK")
    assert out["error"] == "entity_not_found"
    assert "9999.HK" in out["detail"]


def test_events_jp_returns_explicit_unavailable():
    out = _tools(_Session(universe=[JP])).tools["restaurant_company_events"](ticker="7550.T")
    assert out["count"] == 0 and out["events"] == []
    assert out["unavailable"][0]["reason"] == rt.R_EDINET
    assert out["unavailable"][0]["status"] == "unavailable"


def test_events_hk_dedupes_by_doc_url_and_sorts_desc():
    payloads = [
        {"source": "hkex-disclosure", "stock_code": "06862", "release_date": "2026-11-05",
         "ann_type": "monthly-returns", "title_zh": "月报表", "doc_url": "u1"},
        {"source": "hkex-disclosure", "stock_code": "06862", "release_date": "2026-11-05",
         "ann_type": "monthly-returns", "title_zh": "月报表", "doc_url": "u1"},  # 重复
        {"source": "hkex-disclosure", "stock_code": "06862", "release_date": "2026-08-25",
         "ann_type": "results", "title_zh": "中期业绩", "doc_url": "u2"},
        {"source": "hkex-disclosure", "stock_code": "09999", "release_date": "2026-11-01",
         "ann_type": "results", "title_zh": "别家", "doc_url": "u3"},
    ]
    out = _tools(_Session(universe=[HK], events={"hkex-disclosure": payloads})) \
        .tools["restaurant_company_events"](ticker="06862")
    assert out["count"] == 2
    assert [e["disclosure_date"] for e in out["events"]] == ["2026-11-05", "2026-08-25"]
    assert out["events"][0]["source_used"] == "hkex-disclosure"
    assert out["as_of"] == "2026-11-05"


def test_events_limit_is_capped_not_silently_ignored():
    payloads = [{"source": "hkex-disclosure", "stock_code": "06862",
                 "release_date": f"2026-0{(i % 9) + 1}-01", "ann_type": "results",
                 "title_zh": "t", "doc_url": f"u{i}"} for i in range(5)]
    out = _tools(_Session(universe=[HK], events={"hkex-disclosure": payloads})) \
        .tools["restaurant_company_events"](ticker="06862", limit=2)
    assert out["count"] == 2


# ─── 4. financials：市场级不可得 / 期数门 / 未知概念 ────────────────────────
@pytest.mark.parametrize("row,ticker,reason", [
    (HK, "06862", rt.R_HK_PDF),
    (JP, "7550.T", rt.R_EDINET),
    (TW, "2727.TW", rt.R_TWSE),
])
def test_financials_market_unavailable_is_per_concept(row, ticker, reason):
    out = _tools(_Session(universe=[row])).tools["restaurant_company_financials"](ticker=ticker)
    assert [c["count"] for c in out["concepts"]] == [0, 0, 0]
    assert len(out["unavailable"]) == 3, "三个概念各给一条不可得，不是只给一句总的"
    assert {u["reason"] for u in out["unavailable"]} == {reason}


def test_financials_unknown_concept_is_rejected_by_name():
    out = _tools(_Session(universe=[US])).tools["restaurant_company_financials"](
        ticker="MCD", concept="financials.ebitda")
    assert out["error"] == "unknown_concept"
    assert "financials.ebitda" in out["detail"]


def test_financials_short_name_maps_to_full_code_and_returns_points():
    s = _Session(universe=[US], points={"financials.revenue": [
        {"date": "2026-09-30", "value": "7378", "unit": "usd_million",
         "source_used": "sec-edgar-facts", "fetched_at": None, "granularity": "quarter",
         "concept_unit": "usd_million"},
    ]})
    out = _tools(s).tools["restaurant_company_financials"](ticker="MCD", concept="revenue")
    assert out["concepts"][0]["concept"] == "financials.revenue"
    assert out["concepts"][0]["points"][0]["value"] == 7378
    assert out["as_of"] == "2026-09-30"


def test_financials_below_period_gate_says_short_window_not_empty():
    """BROS 营收 19 期 < 24 期门：必须报「期数不足未入库」，不能只说零行。"""
    bros = _universe_row(310002, "BROS", ticker="BROS", region="US",
                         segment="limited-service", lanes=["sec-edgar-facts"])
    out = _tools(_Session(universe=[bros])).tools["restaurant_company_financials"](ticker="BROS")
    reasons = {u["reason"] for u in out["unavailable"]}
    assert rt.R_SHORT_WINDOW in reasons
    detail = " ".join(u.get("detail", "") for u in out["unavailable"])
    assert "24 期" in detail, "必须点名是哪道门，否则读者以为是真实缺测"


# ─── 4b. 同码别名实体（code#id）：库里有就不能报「源侧未覆盖」────────────
CN = {
    "id": 1747, "entity_type": "stock", "code": "603043",
    "name_en": "Guangzhou Restaurant Group Co., Ltd.", "name_zh": "广州酒家",
    "metadata_json": {"ticker": "603043", "region": "CN",
                      "segment": "full-service",
                      "lanes": ["cninfo-operating", "fd-cn-report"]},
}
_CN_POINT = {"date": "2026-09-30", "value": "4431942978.0", "unit": "currency",
             "source_used": "akshare", "fetched_at": None,
             "granularity": "quarter", "concept_unit": "currency"}


def test_financials_reads_same_code_alias_instead_of_reporting_source_gap():
    s = _Session(
        universe=[CN],
        siblings={"603043": [(8227, "603043#8227", "广州酒家")]},
        points_by_entity={8227: {"financials.revenue": [_CN_POINT]}},
    )
    out = _tools(s).tools["restaurant_company_financials"](ticker="603043")
    assert out["concepts"][0]["count"] == 1, "别名实体上的营收必须读得到"
    assert out["observations_from"]["entity_id"] == 8227
    assert out["observations_from"]["code"] == "603043#8227"
    assert "financials.revenue" not in " ".join(
        u.get("detail", "") for u in out["unavailable"]
    ), "读到了就不能再报「源侧未覆盖」"


def test_financials_uses_renamed_alias_but_flags_the_name_mismatch():
    """改名公司（603007 → 顺景科技）也必须读得到，但必须交代名字不符。

    A股/港股的基础代码唯一对应一家上市公司；改名后爬虫以新名字另建实体，
    此时归一名字也对不上。若坚持名字相等，改过名的公司永远读不到。
    """
    s = _Session(
        universe=[CN],
        siblings={"603043": [(9000, "603043#9000", "某某科技")]},
        points_by_entity={9000: {"financials.revenue": [_CN_POINT]}},
    )
    out = _tools(s).tools["restaurant_company_financials"](ticker="603043")
    assert out["concepts"][0]["count"] == 1, "同码改名实体的数据必须读得到"
    assert out["observations_from"]["name_matches"] is False
    notes = " ".join(out["notes"])
    assert "名称与宇宙登记名不同" in notes, "名字不符必须显式写到 notes，不能悄悄借数"


def test_financials_never_borrows_from_a_different_base_code():
    """不同基础代码 = 不同公司。兄弟查询按 `base#%` 圈定，别家实体进不来。"""
    s = _Session(
        universe=[CN],
        siblings={"603043": []},           # 无同码实体
        points={"financials.revenue": []},
        points_by_entity={9000: {"financials.revenue": [_CN_POINT]}},
    )
    out = _tools(s).tools["restaurant_company_financials"](ticker="603043")
    assert out["concepts"][0]["count"] == 0
    assert "observations_from" not in out
    assert {u["reason"] for u in out["unavailable"]} == {"该实体在库内无此概念观测"}


def test_financials_prefers_the_fresher_entity_not_blindly_the_canonical():
    """裸码实体有数据也要看新旧：603043 裸码只到 2025-12-31，别名到 2026-09-30。

    「裸码优先」会稳定返回更旧的数据，而且读者看不出来它旧——故按最新观测日挑。
    """
    stale = {**_CN_POINT, "date": "2025-12-31"}
    fresh = {**_CN_POINT, "date": "2026-09-30"}
    s = _Session(
        universe=[CN],
        siblings={"603043": [(8227, "603043#8227", "广州酒家")]},
        points={"financials.revenue": [stale]},
        points_by_entity={8227: {"financials.revenue": [fresh]}},
    )
    out = _tools(s).tools["restaurant_company_financials"](ticker="603043")
    assert out["concepts"][0]["points"][0]["date"] == "2026-09-30"
    assert out["observations_from"]["code"] == "603043#8227"


def test_financials_keeps_the_canonical_when_it_is_at_least_as_fresh():
    fresh = {**_CN_POINT, "date": "2026-09-30"}
    older = {**_CN_POINT, "date": "2026-06-30"}
    s = _Session(
        universe=[CN],
        siblings={"603043": [(8227, "603043#8227", "广州酒家")]},
        points={"financials.revenue": [fresh]},
        points_by_entity={8227: {"financials.revenue": [older, older]}},
    )
    out = _tools(s).tools["restaurant_company_financials"](ticker="603043")
    assert out["concepts"][0]["points"][0]["date"] == "2026-09-30"
    assert "observations_from" not in out, "裸码实体不旧就该用它"


def test_financial_counts_scopes_aliases_by_base_code():
    """计数 SQL 必须按**基础代码**圈定宇宙。

    第一版修复把合并写成了 Python 侧 pop/re-add，但 SQL 仍然
    ``o.entity_id IN (宇宙裸码实体)``——别名实体的行在 SQL 层就被丢了，
    Python 侧永远 pop 不到，假绿。回归判据钉在发出去的 SQL 上。
    """
    s = _Session(universe=[CN], alias_rows=[])
    _tools(s).tools["restaurant_universe"](region="CN")
    counts_sql = [q for q, _ in s.issued if "financials.revenue" in q and "GROUP BY" in q]
    assert counts_sql, "没有发出财务计数查询"
    assert "split_part(e.code, '#', 1)" in counts_sql[0], (
        "计数必须按基础代码圈定，否则同码别名实体的行会被 SQL 过滤掉"
    )
    assert "e.entity_type = 'stock'" in counts_sql[0], (
        "必须同时限定 entity_type：000721 这类 6 位码在库里还挂着 fund 实体"
        "（兴业货币A），一旦它有财务行就会静默算进西安饮食"
    )


def test_universe_lane_labels_the_real_source_not_a_borrowed_one():
    """车道归因必须对得上真身：财务来自 akshare，经营公告来自 cninfo 条目。

    实测（2026-11-08）：6 家 A股 的 `financials.*` 全是 `source_used=akshare`；
    `cninfo-operating` 全量 224 条成功条目里命中这 6 家 **0** 条。把财务点数
    挂到 cninfo-operating 上，会让读者以为「经营公告车道通了」。
    """
    s = _Session(
        universe=[CN],
        fins={8227: {"financials.revenue": 4,
                     "financials.operating_profit": 4,
                     "financials.net_income": 4}},
        alias_rows=[(8227, "603043#8227", "广州酒家")],
        cninfo={},                      # 该家 0 条经营公告
    )
    out = _tools(s).tools["restaurant_universe"](region="CN")
    lanes = {l["lane"]: l for l in out["entities"][0]["lanes"]}
    assert lanes["fd-cn-report"]["status"] == "data"
    assert lanes["fd-cn-report"]["financial_points"] == 12
    assert "akshare" in lanes["fd-cn-report"]["caveat"], "必须点名真实供源"
    assert lanes["cninfo-operating"]["status"] == "no_data", (
        "cninfo 条目为 0 就不能报 data——财务点数不属于这条车道"
    )
    assert "不发布月度经营数据公告" in lanes["cninfo-operating"]["detail"]


def test_universe_lane_marks_cninfo_data_only_from_crawl_items():
    s = _Session(universe=[CN], cninfo={"603043": 7})
    lanes = {l["lane"]: l
             for l in _tools(s).tools["restaurant_universe"](region="CN")["entities"][0]["lanes"]}
    assert lanes["cninfo-operating"]["status"] == "data"
    assert lanes["cninfo-operating"]["events"] == 7


def test_alias_map_counts_renamed_alias_but_not_a_foreign_code():
    """改名同码 → 计数并入；不同基础代码 → 一律不认。"""
    renamed = _Session(
        universe=[CN],
        fins={9000: {"financials.revenue": 9}},
        alias_rows=[(9000, "603043#9000", "某某科技")],
    )
    lanes = {l["lane"]: l
             for l in _tools(renamed).tools["restaurant_universe"](region="CN")["entities"][0]["lanes"]}
    assert lanes["fd-cn-report"]["status"] == "data", "同码改名实体的行必须并入"

    foreign = _Session(
        universe=[CN],
        fins={9100: {"financials.revenue": 9}},
        alias_rows=[(9100, "603099#9100", "别家公司")],
    )
    lanes = {l["lane"]: l
             for l in _tools(foreign).tools["restaurant_universe"](region="CN")["entities"][0]["lanes"]}
    assert lanes["fd-cn-report"]["status"] == "no_data", "不同基础代码绝不能借数"


# ─── 5. series：未注册 / 零行 / CPI 对照对 / 截断 ──────────────────────────def test_series_unknown_concept_is_unavailable_not_empty():
    out = _tools(_Session(concept=None)).tools["restaurant_series"](code="no.such.code")
    assert out["count"] == 0
    assert out["unavailable"][0]["reason"] == "该序列未注册或不可得"


def test_series_registered_but_no_rows_is_unavailable():
    concept = {"id": 7, "code": "retail.cn.catering.monthly", "name_zh": "餐饮收入",
               "name_en": "Catering revenue", "unit": "cny_100m",
               "frequency": "monthly", "entity_type": "country"}
    out = _tools(_Session(concept=concept)).tools["restaurant_series"](
        code="retail.cn.catering.monthly", source_used="nbs-stats")
    assert out["count"] == 0 and out["points"] == []
    assert "不是真实缺测的断言" in out["unavailable"][0]["detail"]


def test_series_cpi_pair_carries_cross_reference_and_source_windows():
    concept = {"id": 9, "code": "price.cpi.us.food_away_from_home", "name_zh": "外食CPI",
               "name_en": "Food away from home CPI", "unit": "index",
               "frequency": "monthly", "entity_type": "country"}
    s = _Session(
        concept=concept,
        series=[
            {"date": "2026-01", "value": "300.1", "unit": None,
             "source_used": "dbnomics-api", "fetched_at": None, "granularity": "month",
             "entity_type": "country", "entity_id": 1},
        ],
        entity={"code": "USA", "name_zh": "美国", "name_en": "United States"},
        cpi_sources=[("dbnomics-api", "2015-01", "2025-01", 120)],
    )
    out = _tools(s).tools["restaurant_series"](code="price.cpi.us.food_away_from_home")
    joined = " | ".join(out["caveats"])
    assert "price.cpi.us.food_at_home" in joined, "对照对必须写在 caveats 里"
    assert "2025-01" in joined and "冻结" in joined, "镜像冻结窗口要如实注记"
    assert "BLS 官方口 403" in joined
    assert out["entity"]["code"] == "USA"


# ─── 6. sampling notices：类别白名单 + 空命中不假零 ────────────────────────
def test_sampling_notices_rejects_unknown_category():
    out = _tools(_Session()).tools["restaurant_sampling_notices"](category="bad")
    assert out["error"] == "unknown_category"


def test_sampling_notices_empty_category_is_unavailable_not_zero_series():
    out = _tools(_Session()).tools["restaurant_sampling_notices"](category="unqualified")
    assert out["count"] == 0
    assert out["unavailable"][0]["reason"] == "该类别在当前事件面无通告"
    assert "不是零值序列" in out["unavailable"][0]["detail"]


def test_sampling_notices_dedupes_url_and_sorts_desc():
    rows = [
        {"notice_date": "2026-11-01", "title": "A", "url": "u1",
         "result_category": "unqualified", "restaurant_related": True, "body_digest": "d1"},
        {"notice_date": "2026-11-01", "title": "A", "url": "u1",
         "result_category": "unqualified", "restaurant_related": True, "body_digest": "d1"},
        {"notice_date": "2026-11-05", "title": "B", "url": "u2",
         "result_category": "unqualified", "restaurant_related": False, "body_digest": "d2"},
    ]
    out = _tools(_Session(samr=rows)).tools["restaurant_sampling_notices"]("unqualified")
    assert out["count"] == 2 and out["total_matched"] == 2
    assert out["notices"][0]["notice_date"] == "2026-11-05"
    assert out["window"] == {"start": "2026-11-01", "end": "2026-11-05"}


# ─── 7. 读失败语义：绝不降级成"无观测" ────────────────────────────────────
@pytest.mark.parametrize("call", [
    lambda t: t["restaurant_universe"](),
    lambda t: t["restaurant_company_events"](ticker="06862"),
    lambda t: t["restaurant_company_financials"](ticker="MCD"),
    lambda t: t["restaurant_series"](code="x"),
    lambda t: t["restaurant_sampling_notices"](),
])
def test_db_failure_is_error_not_empty_data(call):
    tools = _tools(_Session(fail=True)).tools
    out = call(tools)
    assert out["status"] == "error"
    assert out["error"] == "RuntimeError"
    assert "不是「无观测」" in out["note"]


# ─── 8. 实体解析：三种票号写法都命中同一行 ────────────────────────────────
@pytest.mark.parametrize("ticker", ["06862", "06862.HK", "06862.hk"])
def test_resolve_entity_accepts_bare_and_suffixed_hk_code(ticker):
    s = _Session(universe=[HK])
    row = rt._resolve_entity(s, None, ticker)
    assert row is not None and row["code"] == "06862.HK"


def test_resolve_entity_prefers_entity_id_when_given():
    s = _Session(universe=[HK, US])
    assert rt._resolve_entity(s, 310001, None)["code"] == "MCD"
    assert rt._resolve_entity(s, 999, None) is None


def test_today_is_utc_not_local():
    from datetime import datetime as _dt
    assert rt._today() in {
        _dt.now(timezone.utc).strftime("%Y-%m-%d"),
    }


# ─── 9. 不可得块的形状与人话（防"改成同义常量"的假绿）────────────────────
def test_every_unavailable_block_is_marked_and_human_readable():
    """五个工具在任何不可得路径上都给 {status:"unavailable", reason:<人话>}。

    这里刻意断言**字面片段**而不是比 `rt.R_EDINET`——比常量会在常量被改坏时
    一起绿（自证），字面断言才钉得住对外口径。
    """
    blocks: list[dict] = []
    blocks += _tools(_Session(universe=[JP])).tools["restaurant_universe"]()["entities"][0]["lanes"]
    blocks += _tools(_Session(universe=[JP])).tools["restaurant_company_events"](
        ticker="7550.T")["unavailable"]
    blocks += _tools(_Session(universe=[TW])).tools["restaurant_company_financials"](
        ticker="2727.TW")["unavailable"]
    blocks += _tools(_Session()).tools["restaurant_series"](code="nope")["unavailable"]
    blocks += _tools(_Session()).tools["restaurant_sampling_notices"](
        category="qualified")["unavailable"]

    assert blocks, "至少要有不可得样本，否则本测试是空转"
    for b in blocks:
        if "lane" in b:  # universe 的车道块
            assert b["status"] == "unavailable", b
            assert b.get("reason"), b
        else:
            assert b["status"] == "unavailable", b
            assert b["reason"].strip(), b

    joined = " ".join(b["reason"] for b in blocks)
    assert "EDINET key 未到位" in joined
    assert "台股仅月营收快照" in joined
    assert "该序列未注册或不可得" in joined
    assert "该类别在当前事件面无通告" in joined

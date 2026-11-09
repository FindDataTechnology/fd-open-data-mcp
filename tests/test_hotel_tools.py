"""Hotel read tools — 只读门 + 不可得口径（add-hotel-vertical 任务 5.5）。

三件事必须钉住，否则工具面会变成「看起来有数」的假面：

1. **只读门**：五个工具全部 ``readOnlyHint=True``、``destructiveHint=False``、
   ``openWorldHint=False``（后者尤其重要：工具不现场打上游，只读本服务已存观测）。
2. **基准侧恒不可得**：``hotel_benchmark`` 无论参数如何都返回 unavailable，
   绝不以 0、空数组或同业均值伪装。这是 spec「基准不可得不被伪装成零」的场景。
3. **无车道市场不借数**：欧股（Accor / Whitbread）查 KPI 或事件时必须返回
   「无车道·不可得」，不得返回其他市场同业数值。

用假 session 打桩，不连库。
"""
from __future__ import annotations

import json

import pytest

TOOLS = ("hotel_universe", "hotel_series", "hotel_kpi",
         "hotel_company_events", "hotel_benchmark")


def test_hotel_tools_are_read_only():
    from fd_open_data_mcp.annotations import annotations_for

    for name in TOOLS:
        a = annotations_for(name)
        assert a["readOnlyHint"] is True, name
        assert a["destructiveHint"] is False, name
        assert a["idempotentHint"] is True, name
        assert a["openWorldHint"] is False, name


def test_hotel_tool_surface_is_registered_on_server():
    """server.py 必须真的把这些工具挂上去 —— 实现了但没挂等于没上牌。"""
    from pathlib import Path

    src = (Path(__file__).resolve().parents[1] / "fd_open_data_mcp"
           / "server.py").read_text(encoding="utf-8")
    assert "register_hotel_tools" in src
    assert "register_hotel_tools(mcp)" in src
    # 酒店工具定义不得直接写进 server.py（与 restaurant/flower 同规矩：模块注册）
    assert "def hotel_universe" not in src


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return self

    def all(self):
        return self._rows

    def first(self):
        return self._rows[0] if self._rows else None

    def scalars(self):
        return self

    def __iter__(self):
        return iter(self._rows)


class _FakeSession:
    """按 SQL 关键字路由到预置结果；同时记录是否被关闭。"""

    def __init__(self, entity=None, concept=None, observations=(), items=()):
        self.entity = entity
        self.concept = concept
        self.observations = list(observations)
        self.items = list(items)
        self.closed = False

    def execute(self, statement, params=None):
        sql = str(statement)
        # observations first: their guard embeds a correlated "FROM entities"
        # subquery, so a prefix match on entities would swallow the read.
        if "FROM semantic_observations" in sql:
            return _FakeResult(self.observations)
        if "FROM entities" in sql:
            return _FakeResult([self.entity] if self.entity else [])
        if "FROM concepts" in sql:
            return _FakeResult([self.concept] if self.concept else [])
        if "FROM crawl_items" in sql:
            return _FakeResult(self.items)
        return _FakeResult([])

    def close(self):
        self.closed = True


def _tools(session):
    """把 register_hotel_tools 挂到一个收集器上，返回 {名字: 函数}。"""
    from fd_open_data_mcp.hotel_tools import register_hotel_tools

    collected = {}

    class _MCP:
        def tool(self, annotations=None):
            def deco(fn):
                collected[fn.__name__] = fn
                return fn
            return deco

    register_hotel_tools(_MCP())
    import fd_open_data_mcp.hotel_tools as mod
    mod._session = lambda: session
    return collected


def _entity(code, region, segment="midscale"):
    return {"id": 7, "code": code, "name_zh": "示例公司",
            "metadata_json": json.dumps({"registry": "hotel-universe",
                                         "region": region, "segment": segment,
                                         "lanes": []})}


# ── 基准侧恒不可得（spec 场景：基准不可得不被伪装成零）────────────────────
def test_benchmark_is_always_unavailable_never_zero():
    tools = _tools(_FakeSession())
    out = tools["hotel_benchmark"](metric="revpar", region="CN")
    assert out["benchmark"]["status"] == "unavailable"
    assert "STR" in out["benchmark"]["reason"]
    assert out["benchmark"].get("value") is None
    # 不得出现 0 或空数组冒充
    assert out["benchmark"] != [] and out["benchmark"] != {"value": 0}


def test_benchmark_refuses_peer_average_substitution():
    tools = _tools(_FakeSession())
    out = tools["hotel_benchmark"]()
    assert "同业均值" in out["benchmark"]["detail"] or "peer" in out["benchmark"]["detail"].lower()


# ── 无车道市场如实报出（spec 场景：Accor 无车道）──────────────────────────
def test_eu_company_kpi_reports_no_lane(monkeypatch):
    session = _FakeSession(entity=_entity("AC.PA", "EU"))
    tools = _tools(session)
    out = tools["hotel_kpi"](company_code="AC.PA")
    assert out["unavailable"][0]["status"] == "unavailable"
    assert "无车道" in out["unavailable"][0]["reason"]
    assert "series" not in out  # 不返回任何数值
    assert out["alternatives"]


def test_eu_company_events_reports_no_lane():
    session = _FakeSession(entity=_entity("WTB.L", "EU"))
    tools = _tools(session)
    out = tools["hotel_company_events"](company_code="WTB.L")
    assert out["unavailable"][0]["status"] == "unavailable"
    assert "events" not in out


def test_ota_segment_has_no_hotel_kpi():
    session = _FakeSession(entity=_entity("BKNG", "US", segment="OTA-platform"))
    tools = _tools(session)
    out = tools["hotel_kpi"](company_code="BKNG")
    assert "OTA" in out["unavailable"][0]["reason"]


# ── 序列：空概念给显式不可得，不给空列表 ───────────────────────────────────
def test_series_unknown_concept_is_explicit_unavailable():
    tools = _tools(_FakeSession())
    out = tools["hotel_series"](code="hotel.cn.star_rated.occupancy.all.quarterly")
    assert out["unavailable"][0]["status"] == "unavailable"
    assert "points" not in out


def test_series_points_carry_source_and_caliber_caveats():
    concept = {"id": 1, "code": "hotel.cn.star_rated.occupancy.all.quarterly",
               "name_zh": "平均出租率", "name_en": "occupancy", "unit": "%",
               "frequency": "quarter", "entity_type": "region",
               "category": "行业经营"}
    rows = [
        {"date": "2023-06-30", "value": 52.41, "unit": "%", "entity_type": "region",
         "entity_id": 1, "granularity": "quarter", "source_used": "mct-star-hotel",
         "fetched_at": "2026-11-08T00:00:00Z"},
        {"date": "2024-09-30", "value": 52.96, "unit": "%", "entity_type": "region",
         "entity_id": 1, "granularity": "quarter", "source_used": "mct-star-hotel",
         "fetched_at": "2026-11-08T00:00:00Z"},
    ]
    tools = _tools(_FakeSession(concept=concept, observations=rows))
    out = tools["hotel_series"](code=concept["code"])
    assert out["count"] == 2
    assert out["as_of"] == "2024-09-30"
    assert all(p["source_used"] == "mct-star-hotel" for p in out["points"])
    joined = " ".join(out["caveats"])
    assert "不得据此外推全行业景气" in joined      # 星级口径
    assert "2,209" in joined                        # 50 城小计
    assert "2024Q3" in joined                       # 已停更


def test_series_caps_the_payload_and_says_how_to_get_more():
    """harden-fleet-tool-face-and-queue 3.1: one call must not dump 354 points.

    Live 2026-11-09: a 354-point series produced a 43 KB tool/result, and three
    such calls pushed a 128k-window model into `context pressure (6
    compactions)` — the turn then timed out with no audit trail at all.
    """
    concept = {"id": 1, "code": "hotel.cn.star_rated.occupancy.all.quarterly",
               "name_zh": "平均出租率", "name_en": "occupancy", "unit": "%",
               "frequency": "quarter", "entity_type": "region", "category": "行业经营"}
    rows = [
        {"date": f"{2000 + i}-01-01", "value": float(i), "unit": "%",
         "entity_type": "region", "entity_id": 1, "granularity": "quarter",
         "source_used": "mct-star-hotel", "fetched_at": "2026-11-08T00:00:00Z"}
        for i in range(120)
    ]
    tools = _tools(_FakeSession(concept=concept, observations=rows))

    capped = tools["hotel_series"](code=concept["code"])
    assert capped["count"] == 60, "默认只返回最近 60 点"
    assert capped["total_count"] == 120
    assert capped["truncated"]["total"] == 120
    assert "how_to_get_more" in capped["truncated"]
    assert capped["as_of"] == "2119-01-01", "保留的是最近的点，不是最早的"

    # 显式提高上限即取全量（窗口/limit 是取全量的正路）。
    full = tools["hotel_series"](code=concept["code"], limit=500)
    assert full["count"] == 120
    assert "truncated" not in full


def test_series_read_requires_the_row_to_belong_to_its_entity():
    """A row's declared class must be the class of the entity it hangs off.

    Measured 2026-11-08: 351,740 rows declare a class their entity is not
    (333,975 price/financials rows on fund-manager ``person`` entities, 8,500
    on ``industry``, 936 on a ``fund``). The query joins the entity and compares
    the two, which is the only predicate that separates them from real rows —
    they declare 'stock' exactly like a genuine stock row does.

    Deliberately *not* ``o.entity_type = concept.entity_type``: the star-rated
    survey stores one region-typed concept per city, province and country, so
    city rows sit on city entities (5,565 rows) and pinning them to the
    concept's type made them unreadable.
    """
    captured = {}

    class _S(_FakeSession):
        def execute(self, stmt, params=None):
            text = str(stmt)
            if "FROM concepts" in text:
                class _R:
                    def mappings(self):
                        class _M:
                            def first(_self):
                                return {"id": 1, "code": "hotel.cn.star_rated.occupancy.all.quarterly",
                                        "name_zh": "星级饭店出租率", "name_en": "x", "unit": "%",
                                        "frequency": "quarterly", "entity_type": "industry",
                                        "category": None}
                        return _M()
                return _R()
            captured["sql"] = text
            captured["params"] = params or {}
            return super().execute(stmt, params) if not captured else _Empty()

    class _Empty:
        def mappings(self):
            class _M:
                def all(self):
                    return []
            return _M()

    tools = _tools(_S())
    out = tools["hotel_series"](code="hotel.cn.star_rated.occupancy.all.quarterly",
                                entity_id=2053)
    assert "e.entity_type = o.entity_type" in captured["sql"]
    # a non-security concept must NOT be pinned to its own type
    assert "cet" not in captured["params"]
    assert out["unavailable"], out


def test_series_read_requires_the_entity_to_be_of_the_concept_class():
    """The row's *declared* type is not enough for a security series.

    Measured 2026-11-08: all 343,411 mis-attached price/financials rows declare
    ``entity_type='stock'`` themselves — they sit on person (fund managers),
    industry and fund entities. Only the entity's own class tells them apart, so
    a stock concept read must also require the entity row to be a stock.
    """
    captured = {}

    class _S(_FakeSession):
        def execute(self, stmt, params=None):
            text = str(stmt)
            if "FROM concepts" in text:
                class _R:
                    def mappings(self):
                        class _M:
                            def first(_self):
                                return {"id": 7, "code": "financials.revenue.total",
                                        "name_zh": "营业总收入", "name_en": "revenue",
                                        "unit": "CNY", "frequency": "quarterly",
                                        "entity_type": "stock", "category": None}
                        return _M()
                return _R()
            captured["sql"] = text
            captured["params"] = params or {}

            class _Empty:
                def mappings(self):
                    class _M:
                        def all(self):
                            return []
                    return _M()
            return _Empty()

    tools = _tools(_S())
    out = tools["hotel_series"](code="financials.revenue.total", entity_id=2053)
    assert "EXISTS (SELECT 1 FROM entities e WHERE e.id = o.entity_id" in captured["sql"]
    assert captured["params"]["cet"] == "stock"
    assert out["unavailable"], out

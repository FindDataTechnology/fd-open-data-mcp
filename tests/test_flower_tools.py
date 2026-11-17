"""花卉垂直线五个只读工具的契约测试（add-flower-vertical / task 5.6）。

钉的是**响应契约**，不是 SQL 正确性：

* 五个工具都必须是 ``READ_ONLY``（四项 hint 逐一断言）——这条垂直线对外宣称
  「无写入入口」，上牌时 registry 的 risk 类别靠这四项推导；
* **单位在实体侧**：``flower_price_series`` 必须回品名自己的单位，且不提供
  跨品名「均价」（元/枝 与 元/把 混算是量纲错误）；
* **AIPH 逐行参考年**：``flower_planted_area`` 每行带 ``ref_year``，``year_range``
  按品类分列（混装年份不能被当成年时序）；
* **占比 null ≠ 0**：``flower_company_financials`` 对未披露占比返回
  ``share_unavailable_reason`` + ``unavailable`` 块，绝不返回 0；
* **不可得必须显式**：``flower_trade`` 零行时给 ``R_TRADE_NOT_SCHEDULED``
  而不是空列表；读失败报 ``status:"error"`` 且明写「这不是无观测」。

假会话按已安装源码里的真实 SQL 形状分派（entities 宇宙查询、按 (entity,concept)
计数、按实体取点、AIPH 带 entities join、贸易带 hs_code 过滤），不是顺从假设的空壳。
"""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from fd_open_data_mcp import flower_tools as ft


# ─── 替身：只实现工具真正用到的几件事 ────────────────────────────────────────
class _Result:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return self

    def all(self):
        return list(self._rows)

    def first(self):
        return self._rows[0] if self._rows else None


class _FakeSession:
    """按 SQL 片段分派。任何未识别的形状直接 raise，不静默返回空。"""

    def __init__(self, *, stocks=(), commodities=(), countries=(), counts=(),
                 points=(), area=(), trade=(), segments=(), fail_segments=False):
        self.stocks = list(stocks)
        self.commodities = list(commodities)
        self.countries = list(countries)
        self.counts = list(counts)
        self.points = list(points)
        self.area = list(area)
        self.trade = list(trade)
        self.segments = list(segments)
        self.fail_segments = fail_segments
        self.closed = False
        self.issued: list[str] = []

    def close(self):
        self.closed = True

    def execute(self, clause, params=None):
        sql = " ".join(str(clause).split())
        params = params or {}
        self.issued.append(sql)
        # 注意次序：计数与取点的 SQL 里也含「FROM entities」子查询，
        # 先判具体形状再判宇宙查询，否则会把宇宙行当计数行返回（假绿）。
        # 分部通道：**必须先于**通用 'JOIN entities e ON e.id = o.entity_id' 判，
        # 否则分部查询会被当成 AIPH 面积查询（返回 area 行）。
        if "e.entity_type = 'segment'" in sql:
            if self.fail_segments:
                raise RuntimeError("segment lane down")
            return _Result(self.segments)
        if "GROUP BY o.entity_id, c.code" in sql:
            return _Result(self.counts)
        if "FROM entities" in sql and "metadata_json->>'list'" in sql and "semantic_observations" not in sql:
            if "entity_type = :etype" in sql:
                t = params["etype"]
                pool = {"stock": self.stocks, "commodity": self.commodities,
                        "country": self.countries}[t]
                return _Result(pool)
            return _Result(self.stocks + self.commodities + self.countries)
        # 面积与贸易的 SQL 也带「ORDER BY c.code, o.date DESC」，必须先判 join 形状
        if "JOIN entities e ON e.id = o.entity_id" in sql and "e.entity_type = 'hs_code'" in sql:
            return _Result(self.trade)
        if "JOIN entities e ON e.id = o.entity_id" in sql:
            return _Result(self.area)
        if "row_number() OVER" in sql:      # 逐概念取点（每概念各 limit 条）
            return _Result(self.points)
        raise AssertionError(f"未预期的 SQL 形状：{sql[:160]}")


def _row(eid, etype, code, name_zh, name_en=None, **meta):
    return {"id": eid, "entity_type": etype, "code": code, "name_zh": name_zh,
            "name_en": name_en or name_zh, "metadata_json": meta}


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
    """注册一次，返回 {工具名: 可调用}，并把会话换成替身。"""
    mcp = _FakeMCP()
    ft.register_flower_tools(mcp)

    class _DB:
        def get_session(self):
            return session

    ft.get_database = lambda: _DB()  # type: ignore[assignment]
    return mcp.tools


_STOCKS = [
    _row(1, "stock", "FLWS", "1-800-FLOWERS.COM", "1-800-FLOWERS.COM",
         ticker="FLWS", region="US", segment="cutflower-trade", tier="pure-flower",
         lanes=["sec-edgar-facts"], flower_revenue_share=1.0,
         verified={"ticker": True, "segments": True}),
    _row(2, "stock", "1377.T", "坂田种苗", "Sakata Seed",
         ticker="1377.T", region="JP", segment="breeding-propagation",
         tier="pure-flower", lanes=["edinet-api"], flower_revenue_share=None,
         verified={"ticker": False, "segments": False}),
]

_COMMODITIES = [
    _row(11, "commodity", "dounan-red-rose-single", "红玫瑰（单头）",
         item_raw="红玫瑰（单头）", unit="元/枝", market="kunming-dounan"),
    _row(12, "commodity", "dounan-eustoma", "洋桔梗",
         item_raw="洋桔梗", unit="元/把", market="kunming-dounan"),
]

_COUNTRIES = [
    _row(21, "country", "EC", "厄瓜多尔", source_unit="aiph-stats"),
]


# ─── 注解 ───────────────────────────────────────────────────────────────────
def test_flower_tools_are_read_only():
    from fd_open_data_mcp.annotations import annotations_for

    for name in ("flower_universe", "flower_price_series", "flower_planted_area",
                 "flower_company_financials", "flower_trade"):
        a = annotations_for(name)
        assert a["readOnlyHint"] is True, name
        assert a["destructiveHint"] is False, name
        assert a["idempotentHint"] is True, name
        assert a["openWorldHint"] is False, name


# ─── flower_universe ────────────────────────────────────────────────────────
def test_universe_separates_three_entity_kinds_and_flags_null_share():
    session = _FakeSession(
        stocks=_STOCKS, commodities=_COMMODITIES, countries=_COUNTRIES,
        counts=[(1, "financials.revenue", 12), (11, "price.flower.dounan.avg", 30),
                (21, "area.flower.rose.planted", 3)],
    )
    tools = _tools(session)
    out = tools["flower_universe"]()
    assert session.closed is True
    assert out["counts"] == {"companies": 2, "commodities": 2, "countries": 1}

    flws = next(c for c in out["companies"] if c["code"] == "FLWS")
    assert flws["flower_revenue_share"] == 1.0
    assert flws["share_unavailable_reason"] is None
    assert flws["lanes"][0]["financials"]["status"] == "data"

    sakata = next(c for c in out["companies"] if c["code"] == "1377.T")
    # null 占比：必须给出原因，不得写 0
    assert sakata["flower_revenue_share"] is None
    assert sakata["share_unavailable_reason"] == ft.R_EDINET
    assert sakata["lanes"][0]["financials"]["status"] == "unavailable"

    rose = next(c for c in out["commodities"] if c["code"] == "dounan-red-rose-single")
    assert rose["unit"] == "元/枝"          # 单位取实体，不取概念
    assert rose["lanes"][0]["status"] == "data"
    assert ft.R_DOUNAN_WINDOW in rose["lanes"][0]["caveat"]

    ec = out["countries"][0]
    assert ec["planted_products"][0]["product"] == "玫瑰"
    assert ft.R_AIPH_MIXED in ec["caveat"]

    reasons = " ".join(u["reason"] for u in out["unavailable"])
    assert "KIFA" in reasons and "060311" in reasons


def test_universe_tier_filter():
    tools = _tools(_FakeSession(stocks=_STOCKS))
    out = tools["flower_universe"](tier="pure-flower")
    assert out["counts"]["companies"] == 2
    out = tools["flower_universe"](tier="adjacency")
    assert out["counts"]["companies"] == 0


# ─── flower_price_series ────────────────────────────────────────────────────
def test_price_series_uses_entity_unit_and_never_fills_gaps():
    pts = [
        {"concept_code": "price.flower.dounan.avg", "concept_unit": "currency_cny",
         "date": date(2026, 11, 5), "value": 0.27, "unit": "元/枝",
         "source_used": "dounan-flower-price", "fetched_at": None, "granularity": "day"},
        {"concept_code": "price.flower.dounan.avg", "concept_unit": "currency_cny",
         "date": date(2026, 11, 3), "value": 0.31, "unit": "元/枝",
         "source_used": "dounan-flower-price", "fetched_at": None, "granularity": "day"},
    ]
    tools = _tools(_FakeSession(commodities=_COMMODITIES, points=pts))
    out = tools["flower_price_series"](item="红玫瑰（单头）")
    assert out["unit"] == "元/枝"
    assert out["span"]["distinct_dates"] == 2
    assert len(out["calibers"]["avg"]) == 2
    # 11-04 是区间内的空档：只报不补
    md = out["missing_dates"]
    assert md["count"] == 1 and md["sample"] == ["2026-11-04"]
    assert md["weekday_gaps"] == 1
    assert all(p["date"] != "2026-11-04" for p in out["calibers"]["avg"])
    assert "元/把" in " ".join(out["notes"]) or "单位取品名实体" in " ".join(out["notes"])


def test_price_series_unknown_item_is_explicit():
    tools = _tools(_FakeSession(commodities=_COMMODITIES))
    out = tools["flower_price_series"](item="不存在的花")
    assert out["error"] == "unknown_item"
    assert "flower_universe" in out["hint"]


def test_price_series_zero_rows_reports_unavailable():
    tools = _tools(_FakeSession(commodities=_COMMODITIES, points=[]))
    out = tools["flower_price_series"](entity_id=11)
    assert out["calibers"] == {}
    assert out["unavailable"][0]["status"] == "unavailable"
    assert "零观测" in out["unavailable"][0]["reason"]
    assert "0 行" in out["unavailable"][0]["detail"]


# ─── flower_planted_area ────────────────────────────────────────────────────
def test_planted_area_keeps_per_row_ref_year():
    area = [
        {"concept_code": "area.flower.rose.planted", "concept_unit": "hectare",
         "date": date(2024, 1, 1), "value": 5480, "unit": "hectare",
         "source_used": "aiph-stats", "granularity": "year",
         "country_code": "EC", "name_zh": "厄瓜多尔", "name_en": "Ecuador"},
        {"concept_code": "area.flower.rose.planted", "concept_unit": "hectare",
         "date": date(2012, 1, 1), "value": 2001, "unit": "hectare",
         "source_used": "aiph-stats", "granularity": "year",
         "country_code": "KE", "name_zh": "肯尼亚", "name_en": "Kenya"},
    ]
    tools = _tools(_FakeSession(area=area))
    out = tools["flower_planted_area"](product="rose")
    assert out["count"] == 2
    assert {r["ref_year"] for r in out["rows"]} == {2024, 2012}
    assert out["year_range"]["rose"] == {"min": 2012, "max": 2024, "distinct": 2}
    assert ft.R_AIPH_MIXED in out["notes"][0]
    assert out["countries"] == ["EC", "KE"]


def test_planted_area_rejects_unknown_product():
    tools = _tools(_FakeSession())
    out = tools["flower_planted_area"](product="tulip")
    assert out["error"] == "unknown_product"
    assert "rose" in out["hint"]


def test_planted_area_zero_rows_is_coverage_fact():
    tools = _tools(_FakeSession(area=[]))
    out = tools["flower_planted_area"](product="orchid", country="XX")
    assert out["count"] == 0
    assert "不是零面积" in out["unavailable"][0]["detail"]


# ─── flower_company_financials ──────────────────────────────────────────────
def test_company_financials_null_share_is_not_zero():
    tools = _tools(_FakeSession(stocks=_STOCKS, points=[]))
    out = tools["flower_company_financials"](ticker="1377.T")
    assert out["flower_revenue_share"] is None
    assert out["share_unavailable_reason"] == ft.R_EDINET
    reasons = [u["reason"] for u in out["unavailable"]]
    assert ft.R_EDINET in reasons
    assert any("null" in u["detail"] for u in out["unavailable"])


def test_company_financials_returns_points_and_share():
    pts = [
        {"concept_code": "financials.revenue", "concept_unit": "currency_usd",
         "date": date(2026, 6, 30), "value": 1.2e9, "unit": "currency_usd",
         "source_used": "sec-edgar-facts", "fetched_at": None, "granularity": "quarter"},
    ]
    tools = _tools(_FakeSession(stocks=_STOCKS, points=pts))
    out = tools["flower_company_financials"](ticker="FLWS")
    assert out["company"]["tier"] == "pure-flower"
    assert out["financials"]["营业收入"][0]["value"] == 1.2e9
    assert out["flower_revenue_share"] == 1.0
    assert out["unavailable"] == []
    assert "不可相加" in " ".join(out["notes"])


def test_company_financials_unknown_ticker():
    tools = _tools(_FakeSession(stocks=_STOCKS))
    out = tools["flower_company_financials"](ticker="ZZZZ")
    assert out["error"] == "unknown_entity"


# ─── flower_trade ───────────────────────────────────────────────────────────
def test_trade_zero_rows_declares_lane_not_scheduled():
    tools = _tools(_FakeSession(trade=[]))
    out = tools["flower_trade"]()
    assert len(out["tiers"]) == 6           # 3 档 × 2 流向，恒不合并
    assert out["unavailable"][0]["reason"] == ft.R_TRADE_NOT_SCHEDULED
    assert "不是贸易为零" in out["unavailable"][0]["detail"]


def test_trade_single_flow_and_tier_filter():
    rows = [
        {"hs_code": "060311", "concept_code": "trade.export_flow.world",
         "concept_unit": "currency_usd", "date": date(2024, 1, 1),
         "value": 1.0e8, "unit": "currency_usd", "source_used": "un-comtrade"},
    ]
    tools = _tools(_FakeSession(trade=rows))
    out = tools["flower_trade"](flow="X", hs_code="060311")
    assert len(out["tiers"]) == 1
    assert out["tiers"][0]["flow"] == "X"
    assert out["tiers"][0]["points"][0]["value"] == 1.0e8
    assert "unavailable" not in out
    assert "净出口" in " ".join(out["notes"])


def test_trade_rejects_bad_flow_and_unknown_tier():
    tools = _tools(_FakeSession())
    assert tools["flower_trade"](flow="Z")["error"] == "bad_flow"
    assert tools["flower_trade"](hs_code="999999")["error"] == "unknown_hs_code"


# ─── 读失败不得冒充缺测 ─────────────────────────────────────────────────────
def test_session_failure_is_error_not_empty():
    class _Boom:
        def execute(self, *_a, **_k):
            raise RuntimeError("connection reset")

        def close(self):
            pass

    tools = _tools(_Boom())
    out = tools["flower_universe"]()
    assert out["status"] == "error"
    assert "不是「无观测」" in out["note"]


# ─── 逐概念限额（真库实测缺陷的回归） ──────────────────────────────────────
def test_points_budget_is_per_concept_not_per_call():
    """裸 LIMIT 会被字母序第一个概念吃光（实测 002200 的 revenue 因此为空）。

    钉的是 SQL 形状：必须带 ``row_number() OVER (PARTITION BY c.code …)``，
    否则同一句里三个概念只有一个能拿到行。
    """
    session = _FakeSession(stocks=_STOCKS, points=[])
    tools = _tools(session)
    tools["flower_company_financials"](ticker="FLWS", limit=3)
    point_sql = [q for q in session.issued if "semantic_observations" in q and "concepts" in q]
    assert point_sql, session.issued
    sql = point_sql[0]
    assert "row_number() OVER (PARTITION BY c.code" in sql
    assert ":lim" in sql


def test_price_point_unit_is_entity_unit_not_display_code():
    """观测行 unit 是维度码；逐点必须回品名单位，原始码字留 observed_unit_code。"""
    pts = [{"concept_code": "price.flower.dounan.avg", "concept_unit": "currency_cny",
            "date": date(2026, 11, 5), "value": 0.27, "unit": None,
            "source_used": "dounan-flower-price", "fetched_at": None, "granularity": "day"}]
    tools = _tools(_FakeSession(commodities=_COMMODITIES, points=pts))
    out = tools["flower_price_series"](item="dounan-red-rose-single")
    pt = out["calibers"]["avg"][0]
    assert pt["unit"] == "元/枝"
    assert pt["observed_unit_code"] == "currency_cny"


def test_company_currency_comes_from_region():
    """币种不在观测行里：按注册地补，A股与美股不能读成同一量纲。"""
    pts = [{"concept_code": "financials.revenue", "concept_unit": "currency",
            "date": date(2026, 9, 30), "value": 4.28e8, "unit": "currency",
            "source_used": "akshare", "fetched_at": None, "granularity": "quarter"}]
    a_share = _row(3, "stock", "002200", "交投生态", ticker="002200", region="CN",
                   segment="landscape-nursery", tier="adjacency",
                   lanes=["stock-zygc"], flower_revenue_share=0.0023)
    tools = _tools(_FakeSession(stocks=[a_share], points=pts))
    out = tools["flower_company_financials"](ticker="002200")
    assert out["company"]["currency"] == "CNY"
    assert out["financials"]["营业收入"][0]["value"] == 4.28e8

    tools = _tools(_FakeSession(stocks=_STOCKS, points=pts))
    assert tools["flower_company_financials"](ticker="FLWS")["company"]["currency"] == "USD"


# ─── _missing_dates 单元 ────────────────────────────────────────────────────
def test_missing_dates_counts_weekdays_only():
    md = ft._missing_dates(["2026-11-02", "2026-11-06"])   # 周一 → 周五
    assert md["count"] == 3            # 11-03/04/05
    assert md["weekday_gaps"] == 3
    weekend = ft._missing_dates(["2026-11-06", "2026-11-09"])  # 周五 → 周一
    assert weekend["count"] == 2       # 周六、周日
    assert weekend["weekday_gaps"] == 0
    assert ft._missing_dates([])["count"] == 0


# ─── segments 块（awaken-flower-report-ingest 4.2）──────────────────────────
# 钉三条口径：分部值只来自维度单元；占比是**派生**且带分子/分母；分母找不到时
# 是 null + 原因，不是 0。第四条钉隔离：分部车道读失败不能把合并头条一起判死。

def _seg(entity_code, value, *, d=date(2026, 6, 28), gran="year"):
    return {"entity_code": entity_code, "concept_code": ft.SEGMENT_CONCEPT,
            "concept_unit": "USD", "date": d, "value": value, "unit": "USD",
            "source_used": "sec-xbrl-instance", "granularity": gran, "fetched_at": None}


def _cons(value, *, d=date(2026, 6, 28), gran="year"):
    return {"concept_code": "financials.revenue", "concept_unit": "currency_usd",
            "date": d, "value": value, "unit": "currency_usd",
            "source_used": "sec-edgar-facts", "fetched_at": None, "granularity": gran}


def test_segments_share_is_labelled_derived_with_composition():
    tools = _tools(_FakeSession(
        stocks=_STOCKS, points=[_cons(1_503_511_000)],
        segments=[_seg("FLWS.BloomNetMember", 96_832_000)],
    ))
    out = tools["flower_company_financials"](ticker="FLWS")
    row = out["segments"]["rows"][0]
    assert row["value"] == 96_832_000          # 分部值就是申报值，未经换算
    assert row["source_used"] == "sec-xbrl-instance"
    share = row["revenue_share"]
    assert share["derived"] is True
    assert share["formula"] == "segment_revenue / consolidated_revenue"
    assert share["numerator"]["value"] == 96_832_000
    assert share["numerator"]["concept"] == ft.SEGMENT_CONCEPT
    assert share["denominator"]["value"] == 1_503_511_000
    assert share["denominator"]["concept"] == "financials.revenue"
    assert abs(share["value"] - 96_832_000 / 1_503_511_000) < 1e-6
    # 合并值只作分母引用，不得作为分部值重复交付
    assert row["value"] != share["denominator"]["value"]
    assert "派生" in " ".join(out["segments"]["notes"])


def test_segments_share_null_when_period_not_aligned():
    tools = _tools(_FakeSession(
        stocks=_STOCKS,
        points=[_cons(702_179_000, d=date(2025, 12, 28), gran="quarter")],
        # 分部是 2026-06-28 年度，合并只给了别的期间 ⇒ 分母找不到
        segments=[_seg("FLWS.BloomNetMember", 96_832_000)],
    ))
    out = tools["flower_company_financials"](ticker="FLWS")
    row = out["segments"]["rows"][0]
    assert row["revenue_share"] is None
    assert row["revenue_share_unavailable_reason"]
    assert "%" not in str(row["revenue_share"])   # 不能是 0/0.0 的假值


def test_segments_granularity_mismatch_is_not_compared():
    tools = _tools(_FakeSession(
        stocks=_STOCKS,
        points=[_cons(293_014_000, d=date(2026, 6, 28), gran="quarter")],
        segments=[_seg("FLWS.BloomNetMember", 96_832_000, gran="year")],
    ))
    out = tools["flower_company_financials"](ticker="FLWS")
    row = out["segments"]["rows"][0]
    assert row["revenue_share"] is None
    # 判词必须点名当事的两个粒度值：只写「粒度未对齐」的话，
    # 「这期没披露」与「两车道标成了不同粒度」读起来一模一样，而处置相反。
    reason = row["revenue_share_unavailable_reason"]
    assert "granularity=['quarter']" in reason
    assert "'year'" in reason


def test_segments_missing_denominator_says_which_period():
    """分母根本没有时，判词要点名是哪一期，不能与「粒度不可比」共用一句话。"""
    tools = _tools(_FakeSession(
        stocks=_STOCKS,
        points=[_cons(1_503_511_000, d=date(2026, 9, 27), gran="year")],
        segments=[_seg("FLWS.BloomNetMember", 96_832_000)],
    ))
    out = tools["flower_company_financials"](ticker="FLWS")
    reason = out["segments"]["rows"][0]["revenue_share_unavailable_reason"]
    assert "2026-06-28" in reason
    assert "没有合并营业收入观测" in reason
    assert "粒度不可比" not in reason


def test_segments_zero_rows_is_unavailable_not_empty_ok():
    tools = _tools(_FakeSession(stocks=_STOCKS, points=[_cons(1_503_511_000)]))
    out = tools["flower_company_financials"](ticker="FLWS")
    assert out["segments"]["count"] == 0
    assert out["segments"]["unavailable"][0]["status"] == "unavailable"
    assert "零分部观测" in out["segments"]["unavailable"][0]["reason"]


def test_segment_lane_failure_does_not_kill_consolidated():
    tools = _tools(_FakeSession(
        stocks=_STOCKS, points=[_cons(1_503_511_000)], fail_segments=True,
    ))
    out = tools["flower_company_financials"](ticker="FLWS")
    assert out["segments"]["status"] == "error"
    assert "不是「该公司没有分部披露」" in out["segments"]["note"]
    # 合并头条照常交付：两条车道的可用性彼此独立
    assert out["financials"]["营业收入"][0]["value"] == 1_503_511_000

def test_segments_limit_truncation_is_flagged():
    """LIMIT 撞满时必须自报截断：缺成员与没披露是两件事，观感却完全相同。"""
    tools = _tools(_FakeSession(
        stocks=_STOCKS, points=[_cons(1_503_511_000)],
        segments=[_seg("FLWS.BloomNetMember", 96_832_000)],
    ))
    out = tools["flower_company_financials"](ticker="FLWS", limit=1)
    seg = out["segments"]
    assert seg["truncated"] is True
    assert any("truncated=true" in n for n in seg["notes"])

    out2 = tools["flower_company_financials"](ticker="FLWS", limit=40)
    assert out2["segments"]["truncated"] is False
    assert not any("truncated=true" in n for n in out2["segments"]["notes"])

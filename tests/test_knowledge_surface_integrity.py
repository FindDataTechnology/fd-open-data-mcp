"""基线断言：知识面完整性契约的两条不变量（harden-knowledge-surfaces）。

本文件在**修复之前**加入，按任务 1.2 的要求先跑红，再在 2.x / 3.x 里让它们变绿。
两条断言各自对应 contracts/knowledge-surface-integrity 的一条需求：

- R1 覆盖率与缺口必须实测且声明口径 —— 面级（read/snapshot/history）而非类级。
- R2 退役必须可回答依赖并留下可解析墓碑 —— 退役概念不得整行消失，且要给出依赖清单。

夹具完全复用 design.md 附录 A 的手算示例。
"""

from __future__ import annotations

import pytest

from fd_open_data_mcp.coverage.inventory import coverage_inventory, coverage_summary
from fd_open_data_mcp.models import (
    Concept, ConceptBinding, CrawlPolicy, FetchLog, Function, FunctionColumn,
    SemanticObservation, Source,
)


def _source(session, name: str) -> Source:
    s = Source(name=name, label=name)
    session.add(s)
    session.flush()
    return s


def _function(session, source, command: str, *, verified=True, snap=False, hist=False) -> Function:
    f = Function(
        source_id=source.id, command=command, verified=verified,
        bulk_snapshot=snap, bulk_history=hist,
    )
    session.add(f)
    session.flush()
    return f


def _column(session, fn, name: str) -> FunctionColumn:
    c = FunctionColumn(function_id=fn.id, name=name)
    session.add(c)
    session.flush()
    return c


def _bind(session, concept, col, *, confidence, provenance) -> ConceptBinding:
    b = ConceptBinding(
        concept_id=concept.id, column_id=col.id,
        confidence=confidence, provenance=provenance,
    )
    session.add(b)
    session.flush()
    return b


def _concept(session, *, code="price_close", entity_type="stock",
             frequency="daily", deprecated=False) -> Concept:
    c = Concept(code=code, entity_type=entity_type, frequency=frequency,
                deprecated=deprecated, verified=True, measure="")
    session.add(c)
    session.flush()
    return c


def _three_surface_fixture(session):
    """附录 A 的夹具：三个面都 routable，但只有 read 与 history 面有成功抓取。

    面级「已覆盖」靠 `fetch_log.function_id` 归属——观测行本身不带面标记，
    但抓取日志带（`fetch_log.function_id -> functions.bulk_snapshot/bulk_history`）。
    """
    c = _concept(session)
    a = _source(session, "src-a")
    b = _source(session, "src-b")

    f_read = _function(session, a, "get_realtime")                   # read 面
    f_hist = _function(session, a, "get_series", hist=True)          # b1 -> history
    f_snap = _function(session, b, "get_cross_section", snap=True)    # b2 -> snapshot
    f_unverified = _function(session, b, "get_unverified", verified=False, hist=True, snap=True)

    _bind(session, c, _column(session, f_read, "close"), confidence=0.72, provenance="auto")
    _bind(session, c, _column(session, f_hist, "close"), confidence=0.72, provenance="auto")
    _bind(session, c, _column(session, f_snap, "close"), confidence=0.41, provenance="manual")
    _bind(session, c, _column(session, f_unverified, "close"), confidence=0.95, provenance="auto")

    session.add(SemanticObservation(
        concept_id=c.id, entity_type="stock", entity_id=1,
        date="2026-11-09", granularity="day", value="1", source_used="src-a",
    ))
    # 成功抓取：一次走实时面，一次走 history 面；snapshot 面一次都没成功过
    for fn in (f_read, f_hist):
        session.add(FetchLog(
            source=fn.source_id and "src-a", concept_id=c.id, entity_type="stock",
            entity_id=1, status="ok", function_id=fn.id,
        ))
    session.flush()
    return c


def test_coverage_summary_reports_surfaces_not_just_class_level(session):
    """R1：一个概念有三个 routable 面却只有 read 面有证据时，汇总不得报 100%。

    现状：`coverage_summary` 只聚合 `routable/covered`，`covered == ever_crawled`，
    因此这个夹具会被报成「1/1 covered = 100%」——类级冒充面级。
    """
    _three_surface_fixture(session)

    summary = coverage_summary(session)
    per = summary["per_entity_type"]["stock"]

    # 面级结构必须存在（现状缺失 → 这里先红）
    assert "surfaces" in per, "覆盖率汇总没有面级口径：只有类级 routable/covered"
    assert set(per["surfaces"]) == {"read", "snapshot", "history"}

    # 三个面都 routable，但只有 read 与 history 面有成功抓取
    assert per["surfaces"]["read"]["routable"] == 1
    assert per["surfaces"]["read"]["covered"] == 1
    assert per["surfaces"]["snapshot"]["routable"] == 1
    assert per["surfaces"]["snapshot"]["covered"] == 0
    assert per["surfaces"]["history"]["routable"] == 1
    assert per["surfaces"]["history"]["covered"] == 1

    # 类级口径不得把 2/3 说成满覆盖（这才是「类级冒充面级」的反例）
    total_routable = sum(v["routable"] for v in per["surfaces"].values())
    total_covered = sum(v["covered"] for v in per["surfaces"].values())
    assert (total_covered, total_routable) == (2, 3), (
        f"面级覆盖被压成一个数：{total_covered}/{total_routable} 应报 2/3"
    )
    assert summary["surfaces"]["snapshot"] == {
        "routable": 1, "covered": 0, "never_attempted": 0}


def test_coverage_summary_declares_granularity_and_class(session):
    """R1：申报必须带口径标签（粒度 + 分类维度），否则数字不可用。"""
    _three_surface_fixture(session)

    summary = coverage_summary(session)
    assert summary.get("granularity") == "surface", "汇总未声明粒度为面级"
    assert "by_entity_type" in summary or "per_entity_type" in summary


def test_coverage_gap_counts_by_class(session):
    """R1：缺口按类别分计，不得把不同类别混成一个总数后当单一事实。

    概念层的类别维度是 `entity_type`（股票 / 基金 / 国家 …）。证券类别
    （A 股 / ETF / 北交所）属于**实体层**（代码前缀），概念层的覆盖报告
    不承载该维度——那是 `entities` / `entity_source_identifiers` 的活。
    """
    for code, et in (("close", "stock"), ("nav", "fund"), ("close_bj", "stock")):
        c = _concept(session, code=code, entity_type=et)
        col = _column(session, _function(session, _source(session, f"src-{code}"), f"fn-{code}"), "v")
        _bind(session, c, col, confidence=0.9, provenance="auto")

    summary = coverage_summary(session)
    per = summary["per_entity_type"]
    assert per["stock"]["routable"] == 2
    assert per["fund"]["routable"] == 1
    # 分类维度必须随汇总一起申报，否则「2 个股票概念 + 1 个基金概念」会被读成「3 个」这一个事实
    assert summary.get("class_dimension") == "entity_type"


def test_deprecated_concept_stays_resolvable_with_dependents(session):
    """R2：退役概念不得从清单整行消失，且必须能答出依赖它的策略。

    现状：`coverage_inventory` 用 `filter(Concept.deprecated.is_(False))` 把退役概念
    整行丢弃——依赖它的策略因此静默变成 unroutable，且无处可查（54 天静默的落点）。
    """
    c = _concept(session, code="retired_indicator", deprecated=True)
    col = _column(session, _function(session, _source(session, "src-r"), "fn-r"), "v")
    _bind(session, c, col, confidence=0.9, provenance="auto")
    policy = CrawlPolicy(
        name="p-uses-retired", concept_ids=[c.id], entity_type="stock",
        date_policy={"mode": "trailing", "days": 7}, frequency="daily",
        cron_expr="0 3 * * *", timezone="UTC",
    )
    session.add(policy)
    session.flush()

    rows = coverage_inventory(session, routable_only=False)
    row = next((r for r in rows if r["code"] == "retired_indicator"), None)

    assert row is not None, "退役概念从覆盖清单整行消失：无法解析、无法回答谁依赖它"
    assert row["deprecated"] is True
    assert row["dependents"], "退役概念没有依赖清单，退化的刀无法知道爆炸半径"
    assert any(d["policy_id"] == policy.id for d in row["dependents"])


# --- R2：退役必须可回答依赖、留墓碑、且不自动改指向 ----------------------- #


def test_retirement_is_refused_while_a_live_policy_depends_on_it(session):
    from fd_open_data_mcp.coverage.retirement import RetirementBlocked, retire_concept

    c = _concept(session, code="to_retire")
    session.add(CrawlPolicy(
        name="live-p", concept_ids=[c.id], entity_type="stock",
        date_policy={"mode": "trailing", "days": 7}, frequency="daily",
        cron_expr="0 3 * * *", timezone="UTC", enabled=True,
    ))
    session.flush()

    with pytest.raises(RetirementBlocked) as ei:
        retire_concept(session, c.id)
    assert ei.value.dependents["policies"][0]["name"] == "live-p"
    assert session.get(Concept, c.id).deprecated is False, "拒绝退役时不得已经落盘"


def test_forced_retirement_leaves_the_tombstone_and_never_rebinds(session):
    from fd_open_data_mcp.coverage.retirement import retire_concept

    c = _concept(session, code="force_retire")
    p = CrawlPolicy(
        name="p-keeps-old-id", concept_ids=[c.id], entity_type="stock",
        date_policy={"mode": "trailing", "days": 7}, frequency="daily",
        cron_expr="0 3 * * *", timezone="UTC", enabled=True,
    )
    session.add(p)
    session.flush()

    out = retire_concept(session, c.id, force=True)
    assert out["deprecated"] is True
    assert session.get(Concept, c.id) is not None, "退役不是删除：行必须留下"

    # 不做自动重定向：策略仍然指向原概念，由人决定后续
    session.refresh(p)
    assert p.concept_ids == [c.id]


def test_retired_concept_stays_resolvable_with_successor(session):
    """读路径必须能与「不存在」区分：已退役 + 后继，而不是未找到。"""
    from fd_open_data_mcp.entities.resolver import ConceptDeprecated, check_applicability

    old = _concept(session, code="price.open", deprecated=True)
    old.name_zh = "开盘价"
    new = _concept(session, code="price.open.v2")
    new.name_zh = "开盘价"
    session.flush()

    with pytest.raises(ConceptDeprecated) as ei:
        check_applicability(session, old.id, "stock")
    assert ei.value.replacement is not None
    assert ei.value.replacement.id == new.id

    with pytest.raises(ValueError) as ei2:
        check_applicability(session, 999999, "stock")
    assert not isinstance(ei2.value, ConceptDeprecated), "未找到与已退役必须可区分"


def test_retired_concept_is_not_a_crawl_gap(session):
    """退役概念留在清单里，但不得进入抓取缺口集——否则会去爬一个退役指标。"""
    from fd_open_data_mcp.coverage.inventory import gap_set

    c = _concept(session, code="retired_gap", deprecated=True)
    col = _column(session, _function(session, _source(session, "src-g"), "fn-g"), "v")
    _bind(session, c, col, confidence=0.9, provenance="auto")
    session.flush()

    assert all(r["code"] != "retired_gap" for r in gap_set(session))


# --- R1 尾部：不可得 vs 零；新旧口径并行 --------------------------------- #


def test_never_attempted_surface_is_not_reported_as_zero(session):
    """没试过的面必须报 nunca 尝试，不得落成 0——否则「没证据」变成「证明没有」。"""
    c = _concept(session, code="never_tried")
    col = _column(session, _function(session, _source(session, "src-n"), "fn-n", snap=True), "v")
    _bind(session, c, col, confidence=0.9, provenance="auto")
    session.flush()   # 完全没有 fetch_log

    summary = coverage_summary(session)
    assert summary["surfaces"]["snapshot"] == {
        "routable": 1, "covered": 0, "never_attempted": 1}


def test_attempted_but_failed_surface_is_zero_not_never_attempted(session):
    """试过但失败 = 真 0；与「没试过」必须分开。"""
    c = _concept(session, code="tried_failed")
    fn = _function(session, _source(session, "src-f"), "fn-f", snap=True)
    _bind(session, c, _column(session, fn, "v"), confidence=0.9, provenance="auto")
    session.add(FetchLog(source="src-f", concept_id=c.id, entity_type="stock",
                         entity_id=1, status="timeout", function_id=fn.id))
    session.flush()

    summary = coverage_summary(session)
    assert summary["surfaces"]["snapshot"] == {
        "routable": 1, "covered": 0, "never_attempted": 0}


def test_legacy_class_level_figure_is_kept_and_labelled(session):
    """旧类级数字保留给既有消费方，但必须带口径标签。"""
    _three_surface_fixture(session)

    summary = coverage_summary(session)
    assert summary["legacy"]["covered"] == summary["covered"]
    assert summary["legacy"]["granularity"] == "concept"
    assert "do not read as surface-level" in summary["legacy"]["note"]


# --- R3：注册表读路径的三态（未接线 / 上游不可用 / 空）必须可分辨 ----------


def test_registry_read_reports_not_wired_when_table_absent(session):
    """表不存在 = 未接线，是受支持的部署形态，不是故障。"""
    from fd_open_data_mcp.semantic.registry_catalog import (
        REGISTRY_NOT_WIRED, read_verified_entries_state,
    )

    entries, state = read_verified_entries_state(session)
    assert entries == []
    assert state == REGISTRY_NOT_WIRED, f"缺表应报未接线，实际 {state}"


def test_registry_read_reports_unavailable_on_read_error(session, monkeypatch):
    """表在但读不了 = 上游不可用——此前与「表是空的」共用同一个 []。"""
    from fd_open_data_mcp.semantic.registry_catalog import (
        REGISTRY_UNAVAILABLE, read_verified_entries_state,
    )
    from sqlalchemy.exc import OperationalError

    monkeypatch.setattr(
        "fd_open_data_mcp.semantic.registry_catalog.registry_table_exists",
        lambda _s: True,
    )

    class Boom:
        def execute(self, *_a, **_k):
            raise OperationalError("select", {}, Exception("db gone"))

    entries, state = read_verified_entries_state(Boom())
    assert entries == []
    assert state == REGISTRY_UNAVAILABLE, f"读失败应报不可用，实际 {state}"


def test_registry_read_distinguishes_empty_from_unavailable(session, monkeypatch):
    """同样是空列表，状态必须不同——R3「MUST NOT 共用同一形状」。

    直接造两种现实：表在但读失败 vs 表在且确实没有行。两者 entries 都是 []，
    唯一能区分的就是 state。这条断言正是那个区分本身。
    """
    from fd_open_data_mcp.semantic import registry_catalog as rc
    from sqlalchemy.exc import OperationalError

    monkeypatch.setattr(rc, "registry_table_exists", lambda _s: True)

    class Boom:
        def execute(self, *_a, **_k):
            raise OperationalError("select", {}, Exception("db gone"))

    empty_entries, empty_state = rc.read_verified_entries_state(Boom())

    class Empty:
        class _Res:
            @staticmethod
            def mappings():
                class _M:
                    @staticmethod
                    def all():
                        return []
                return _M()
        def execute(self, *_a, **_k):
            return self._Res()

    real_entries, real_state = rc.read_verified_entries_state(Empty())

    assert empty_entries == real_entries == []
    assert empty_state == rc.REGISTRY_UNAVAILABLE
    assert real_state == rc.REGISTRY_OK
    assert empty_state != real_state, "读失败与真的没数据必须可分辨"


# --- R3：缓存未命中 ≠ 无数据（读穿透的覆盖申报必须三态可分辨）--------------


def test_cold_series_reports_unknown_not_empty():
    """没有任何点 = 什么都不知道 → 整个窗口可抓取，而不是「这里没有数据」。

    这是 R3 的核心反例：把「还没取过」申报成「取过且确实没有」，下游就会
    永久跳过这段。
    """
    from fd_open_data_mcp.fetch.coverage import coverage

    cov = coverage([], "2026-01-01", "2026-01-31")
    assert cov.cold is True, "无任何点应判为 cold"
    assert cov.complete is False
    assert cov.fetchable == (("2026-01-01", "2026-01-31"),), "整窗都应被视为可抓取"
    assert cov.known_empty == (), "还没取过，不能声称「确实没有」"


def test_probed_empty_is_not_refetched_but_is_reported():
    """探过且确实为空（休市）→ 仍在 gaps 里被申报，但不进 fetchable。"""
    from fd_open_data_mcp.fetch.coverage import coverage

    # 整段都是休市：探过、为空
    cov = coverage([], "2026-02-14", "2026-02-20", known_empty=[("2026-02-14", "2026-02-20")])
    assert cov.known_empty == (("2026-02-14", "2026-02-20"),), "必须申报「探过为空」"
    assert cov.fetchable == (), "探过为空的区间不得反复抓取"


def test_three_coverage_states_are_mutually_distinguishable():
    """未取过 / 取过确实没有 / 窗口完整 —— 三种事实必须可区分。"""
    from fd_open_data_mcp.fetch.coverage import coverage

    unknown = coverage([], "2026-01-01", "2026-01-10")          # 什么都没取过
    probed_empty = coverage([], "2026-01-01", "2026-01-10",
                            known_empty=[("2026-01-01", "2026-01-10")])  # 取过，确实没有
    complete = coverage(
        [f"2026-01-{d:02d}" for d in range(1, 11)], "2026-01-01", "2026-01-10"
    )                                                            # 全都有

    # 三者 gaps 形状相近，但 fetchable / known_empty / complete 各不相同
    assert unknown.fetchable != probed_empty.fetchable, "未取过与取过为空必须可区分"
    assert probed_empty.known_empty and not unknown.known_empty
    assert complete.complete is True and unknown.complete is False


# --- R1：未探测时申报「不可得」，不得用 0 代替 ----------------------------------


def test_empty_catalog_is_not_measurable_not_zero_percent(session):
    """目录空 = 没有可测的东西，不是「0% 覆盖」。

    两者在计数器上都是 0，但一个是「探过了，结果很差」，另一个是「根本没得探」。
    R1 要求前者报数字、后者报不可得并说明缺什么。
    """
    from fd_open_data_mcp.coverage.inventory import coverage_summary

    summary = coverage_summary(session)
    assert summary["total_concepts"] == 0
    assert summary["state"] == "not_measurable",         "空目录必须申报不可得，不能用 0% 代替"
    assert summary["missing"], "不可得必须说明缺什么"
    assert "catalog" in summary["missing"][0]


def test_populated_catalog_is_measured_even_when_nothing_is_routable(session):
    """有概念但一个都不可路由 = 真实测得的 0%，不是「不可得」。"""
    from fd_open_data_mcp.coverage.inventory import coverage_summary

    _concept(session, code="unbound_concept")   # 无绑定 → 不可路由
    summary = coverage_summary(session)
    assert summary["total_concepts"] == 1
    assert summary["state"] == "measured", "有目录就是能测，哪怕结果是 0"
    assert summary["routable"] == 0

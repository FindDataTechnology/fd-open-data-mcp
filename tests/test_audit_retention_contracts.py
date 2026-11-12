"""留存契约审计的判定逻辑（纯函数）——这是整套原则里最容易被写成"恒真"的一段。

重点守三件事：
  1. 契约缺失必须算违规，不能被默认成"合规"；
  2. T 类（只留当期）若仍在逐日堆积，必须报——这是防复发判据，写成恒 False 就等于没写；
  3. 未知/读不到时退出码是 2，不是 0。
"""

import importlib.util
import pathlib
import sys

SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "audit_retention_contracts.py"


def load():
    spec = importlib.util.spec_from_file_location("audit_rc", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    # 必须先入 sys.modules：脚本用了 `from __future__ import annotations` + @dataclass，
    # dataclass 解析字符串注解时要回查 cls.__module__ 对应的模块对象，缺了就会抛
    # AttributeError: 'NoneType' object has no attribute '__dict__'。
    sys.modules["audit_rc"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_resolve_prefers_exact_then_source_wildcard_then_global(tmp_path=None):
    mod = load()
    # 精确命中
    c = mod.resolve_contract("akshare", "financials")
    assert c.cls == "F" and c.window_days is None
    # 源级通配
    c = mod.resolve_contract("worldbank", "gdp")
    assert c.cls == "F"
    # 未知源 → None（不是默认合规）
    assert mod.resolve_contract("no-such-source", "whatever") is None


def test_missing_contract_is_a_violation():
    mod = load()
    v = mod.classify_violations(None, rows=100, over_window=0,
                                rows_per_entity_recent=0.0, family="x")
    assert v and v[0].startswith("no_contract")


def test_t_class_still_accruing_daily_rows_is_flagged():
    """核心防复发判据：T 类声明只留当期，若每实体每天一行就是违规。"""
    mod = load()
    c = mod.resolve_contract("akshare", "tenure_days")
    assert c.cls == "T"
    v = mod.classify_violations(c, rows=216913, over_window=0,
                                rows_per_entity_recent=7.0, family="tenure_days")
    assert any(x.startswith("T_class_accruing_history") for x in v)


def test_t_class_at_single_snapshot_is_clean():
    """同一实体近 7 天只有 1 行（真正只留当期）就不该报。"""
    mod = load()
    c = mod.resolve_contract("akshare", "tenure_days")
    v = mod.classify_violations(c, rows=4000, over_window=0,
                                rows_per_entity_recent=1.0, family="tenure_days")
    assert v == []


def test_over_window_on_windowed_class_is_flagged():
    mod = load()
    c = mod.resolve_contract("akshare", "aum")   # W
    v = mod.classify_violations(c, rows=832036, over_window=424969,
                                rows_per_entity_recent=0.0, family="aum")
    assert any(x.startswith("over_window") for x in v)


def test_permanent_class_must_not_carry_a_window():
    """P 类不可回收，若**契约里**被标了窗口就是自相矛盾。

    注意判据是 contract.window_days，不是 over_window——P 类有五年以上的老行
    恰恰是对的。旧实现用 over_window 判，把 7 个正常 P 族误报成违规
    （2026-11-11 实测：gleif-lei / openalex-works / openfda …）。
    """
    mod = load()
    c = mod.Contract("x", "crawl", "P", 365, False, True, 64)
    v = mod.classify_violations(c, rows=10, over_window=5,
                                rows_per_entity_recent=0.0, family="crawl")
    assert any(x.startswith("P_class_has_window") for x in v)


def test_permanent_class_with_old_rows_is_fine():
    """反向守卫：P 类有大量超五年老行不算违规——它就是该一直留着。"""
    mod = load()
    c = mod.Contract("gleif-lei", "entities", "P", None, False, True, 64)
    v = mod.classify_violations(c, rows=81, over_window=81,
                                rows_per_entity_recent=0.0, family="entities")
    assert v == []


def test_over_budget_is_flagged():
    mod = load()
    c = mod.Contract("s", "f", "F", None, True, True, 1)  # 预算 1MB
    v = mod.classify_violations(c, rows=100_000, over_window=0,
                                rows_per_entity_recent=0.0, family="f")
    assert any(x.startswith("over_budget") for x in v)


def test_audit_is_read_only():
    """静态守卫：审计不得出现写语句。"""
    src = SCRIPT.read_text().upper()
    for kw in ("DELETE FROM", "TRUNCATE", "UPDATE SEMANTIC_OBSERVATIONS", "INSERT INTO"):
        assert kw not in src


def test_severity_separates_capacity_from_contract_backlog():
    """容量违规与'契约待补'必须分档——混在一个退出码里会让信号失效。

    实测 2026-11-11：111 个 (源,族) 里 72 个是 <1MB 的 no_contract，
    真正的容量风险只有 6 个族。若两者同权，CI 永久红灯、没人再看。
    """
    mod = load()
    assert mod.severity_of([]) == "ok"
    assert mod.severity_of(["no_contract：x"]) == "contract_gap"
    # 有任意一条非 no_contract 的违规 → 升级为容量问题
    assert mod.severity_of(
        ["no_contract：x", "T_class_accruing_history：y"]) == "capacity"
    assert mod.severity_of(["over_budget：z"]) == "capacity"


def test_severity_is_not_constant():
    """反向守卫：如果实现里恒返回某一档，这三条断言会同时失败。"""
    mod = load()
    got = {mod.severity_of([]), mod.severity_of(["no_contract：a"]),
           mod.severity_of(["over_window：b"])}
    assert got == {"ok", "contract_gap", "capacity"}


def test_recent_rows_denominator_is_per_concept_entity():
    """判据的分母必须是 (概念, 实体)，不是 (实体)。

    2026-11-11 实测：nav 族有 3 个概念、performance 有 4 个共用同一批实体。
    按 (实体) 做分母时，T 类正确执行（每概念每实体 1 行）会被算成 2.84 / 2.02 行，
    判成"仍在逐日堆积"——**数据是对的，判据错了**。这种假红会让人把闸门关掉。
    """
    src = SCRIPT.read_text()
    assert "count(DISTINCT (o.concept_id, o.entity_type, o.entity_id))" in src, \
        "近 7 天每实体行数的分母必须按 (概念, 实体) 计"
    # 反向守卫：旧的 (实体) 分母不能再出现在 per_entity 的表达式里
    assert "GREATEST(count(DISTINCT (o.entity_type, o.entity_id)), 1) AS per_entity" not in src


def test_over_window_uses_contract_window_not_hardcoded_5y():
    """超窗判据必须按契约自己的 window_days 切。

    旧实现写死 `interval '1826 days'`（5 年），文案却打印契约的窗口天数：
    W/730 的族会漏报，报出来的行数又与文案对不上。窗口取值改成从契约表生成。
    """
    src = SCRIPT.read_text()
    assert "interval '1826 days'" not in src, \
        "不得把 5 年窗口写死进 SQL；应按契约的 window_days 生成"
    assert "for w in windows" in src


def test_t_class_over_window_is_not_a_capacity_violation():
    """T 类即使快照比窗口旧也不得判成超窗。

    T 类每个 (概念, 实体) 只有一条快照；按窗口把它删掉，等于把"陈旧"变成"没有"，
    而"未知 ≠ 空"是本仓反复踩的坑（读不到当成不存在）。陈旧是取数覆盖问题，
    由巡检/覆盖度那侧报，不进容量违规——否则闸门永远红，最后被人关掉。
    """
    mod = load()
    # 五项齐备才算"合规契约"。本用例判的是超窗口径，不是完整性，
    # 所以 cost 要显式给——否则会顺带命中 contract_incomplete 而分不清在验哪条。
    c = mod.Contract("akshare", "yield", "T", 30, True, False, 64,
                     re_fetch_cost="one_http")
    v = mod.classify_violations(c, rows=998, over_window=898,
                               rows_per_entity_recent=1.0, family="yield")
    assert v == [], f"T 类不该因超窗报容量违规，实际 {v}"


def test_w_class_over_window_still_flagged():
    """反向守卫：W 类超窗必须继续报（这条不能被上面的放宽顺手带掉）。"""
    mod = load()
    c = mod.Contract("akshare", "aum", "W", 1826, True, True, 512)
    v = mod.classify_violations(c, rows=407301, over_window=234,
                               rows_per_entity_recent=7.0, family="aum")
    assert any(x.startswith("over_window") for x in v)


def test_t_w_without_re_fetchable_is_a_contract_gap_not_capacity():
    """T/W 的 `re_fetchable` 未声明要报，但**不能**升级成容量事故。

    这个字段决定"删了能不能拿回来"，空着就是缺口；可它也不是在吃盘。混进 capacity
    会让 CI 永久红灯（本仓已经因为这种事把闸门关过一次），所以必须落在 contract_gap。
    """
    mod = load()
    c = mod.Contract("akshare", "performance", "T", None, None, None, 64)
    v = mod.classify_violations(c, rows=602, over_window=0,
                                rows_per_entity_recent=1.0, family="performance")
    assert any(x.startswith("contract_incomplete") for x in v)
    assert mod.severity_of(v) == "contract_gap"     # 不是 capacity
    # 显式 false / true（且五项齐备）都不报这条
    for flag in (False, True):
        c2 = mod.Contract("s", "f", "T", None, flag, None, 64,
                          re_fetch_cost="one_http" if flag else "impossible")
        v2 = mod.classify_violations(c2, rows=10, over_window=0,
                                     rows_per_entity_recent=1.0, family="f")
        assert not any(x.startswith("contract_incomplete") for x in v2)


def test_t_w_without_re_fetch_cost_names_the_third_state():
    """R1 的第三项（`re_fetch_cost`）缺了也要报，且同样只算契约缺口。

    为什么单独一条：布尔 `re_fetchable` 说不出"能取回但很贵"，而这正是 aum 唯一
    诚实的描述（可重取，但要 1,128 小时）。缺这一项时淘汰工具只能退回布尔判断，
    等于丢掉"贵不贵"这一档——2026-11-12 之前库里就是这个状态（alembic 0023 才补列）。

    守卫要求：报出来必须**点名三态**，否则读的人不知道该怎么填。
    """
    mod = load()
    c = mod.Contract("akshare", "aum", "W", 1826, True, True, 512)   # 只有布尔
    v = mod.classify_violations(c, rows=407067, over_window=0,
                                rows_per_entity_recent=0.0, family="aum")
    hits = [x for x in v if x.startswith("contract_incomplete") and "re_fetch_cost" in x]
    assert hits, f"缺 re_fetch_cost 必须被点名，实际 {v}"
    assert "rebuild" in hits[0] and "one_http" in hits[0], \
        f"要说清三态怎么填，实际：{hits[0]}"
    assert mod.severity_of(v) == "contract_gap", "缺字段不是容量事故，别让闸门永久红"
    # 填了三态就不该再报
    c2 = mod.Contract("akshare", "aum", "W", 1826, True, True, 512,
                      re_fetch_cost="rebuild", re_fetch_cost_hours=1128)
    v2 = mod.classify_violations(c2, rows=407067, over_window=0,
                                 rows_per_entity_recent=0.0, family="aum")
    assert not any("re_fetch_cost" in x for x in v2)


def test_cost_is_co_presented_with_over_window():
    """R5：报"能释放多少"时必须同时报"要重算多久"。

    只报容量不报代价，就会得出"为了 77 kB 删掉要重算 1,128 小时的数据"这种账——
    aum 就是那个实例（2026-11-12 实测：整族 38 MB、超窗部分 77 kB、重算 47 天）。
    """
    mod = load()
    c = mod.Contract("akshare", "aum", "W", 1826, True, True, 512,
                     re_fetch_cost="rebuild", re_fetch_cost_hours=1128)
    v = mod.classify_violations(c, rows=407067, over_window=222,
                                rows_per_entity_recent=0.0, family="aum")
    hit = [x for x in v if x.startswith("over_window")][0]
    assert "1128" in hit, f"超窗提示必须带重算成本，实际：{hit}"

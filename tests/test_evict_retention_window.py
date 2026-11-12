"""窗口淘汰工具的契约：默认不动数据、只碰 T/W、可逆。

这个脚本是唯一会减少 `semantic_observations` 行数的地方，所以测试重点不在"搬得对不对"，
而在三条不可越过的边界：
  1. 默认 dry-run（不跑 --apply 一行都不动）；
  2. P / F 与无契约族绝不进入候选；
  3. 搬运写进 quarantine、保留原 id（回滚能精确到行）。
"""

import importlib.util
import pathlib
import sys

SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "evict_retention_window.py"


def load():
    spec = importlib.util.spec_from_file_location("evict_rw", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["evict_rw"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_t_class_keeps_only_latest_point_per_entity():
    """T 类：同一 (概念, 实体) 只留 date 最大的一条，其余都是 rn>1。"""
    sql = load()._victims_sql("T")
    assert "row_number()" in sql
    assert "PARTITION BY o.concept_id, o.entity_type, o.entity_id" in sql
    assert "rn > 1" in sql


def test_w_class_cuts_by_window_lower_bound():
    sql = load()._victims_sql("W")
    assert "o.date < :cutoff" in sql
    assert "split_part(c.code, '.', 1) = :fam" in sql


def test_p_and_f_are_not_supported_by_the_victim_selector():
    """P/F 不该有淘汰语句——调用方也只对 T/W 调它；这里再加一道。"""
    mod = load()
    for cls in ("P", "F"):
        try:
            mod._victims_sql(cls)
        except ValueError as exc:
            assert cls in str(exc)
        else:
            raise AssertionError(f"{cls} 不应能构造淘汰语句")


def test_script_default_is_dry_run():
    """源码层面：--apply 是可选开关，且 dry-run 判断出现在任何 INSERT 之前。"""
    src = SCRIPT.read_text()
    assert '"--apply", action="store_true"' in src
    dry = src.index("if not a.apply:")
    insert = src.index("INSERT INTO semantic_observations_quarantine")
    assert dry < insert, "dry-run 判断必须早于搬运语句"


def test_quarantine_insert_preserves_original_id_and_fills_not_null():
    """隔离表 id 无序列（实测 id_default=NONE），因此必须沿用原 id；
    且 declared_type / entity_actual_type / reason / batch_id 都是 NOT NULL。"""
    src = SCRIPT.read_text()
    assert "SELECT o.id, o.concept_id" in src
    assert "ON CONFLICT (id) DO NOTHING" in src
    for col in ("declared_type", "entity_actual_type", "reason", "batch_id"):
        assert col in src


def test_batch_delete_is_by_id_not_by_predicate():
    """删除必须按已搬走的 id 列表做，不能再按原判据——否则并发写入会被误删。"""
    src = SCRIPT.read_text()
    assert "DELETE FROM semantic_observations WHERE id IN" in src


def test_refuses_families_declared_not_re_fetchable():
    """4.1：`re_fetchable is False` 才是"不可回收"，必须拒绝。

    三态，不能写成 `if not ct.re_fetchable`：那会把 **未声明（NULL）** 也当成
    "不可回收"。2026-11-11 实测：T/W 里显式 false 的一个都没有，未声明的有 3 个
    （polygon/employee_count、polygon/market_capitalization、akshare/performance），
    旧写法把这三族全拒了、还把文案说成"声明 false"——报出去的是一句假话。
    """
    src = SCRIPT.read_text()
    assert "if ct.re_fetchable is False:" in src, "必须只对显式 false 拒绝"
    assert "if not ct.re_fetchable:" not in src, "不得把 NULL 当成不可回收"
    assert "不可回收层" in src
    assert "re_fetchable 未声明" in src, "未声明要单独提示，不能沉默"


def test_apply_asserts_row_count_invariant():
    """4.2：搬运前后必须对账（观测 −N / 隔离 +N / 修订 ±0），对不上就抛。"""
    mod = load()
    before = {"semantic_observations": 100, "semantic_observations_quarantine": 10,
              "observation_revisions": 5}
    ok = {"semantic_observations": 90, "semantic_observations_quarantine": 20,
          "observation_revisions": 5}
    mod._assert_balanced(before, ok, 10)      # 不应抛

    # 少删了一行（丢数据/漏处理）
    bad_del = dict(ok, semantic_observations=91)
    try:
        mod._assert_balanced(before, bad_del, 10)
    except RuntimeError as exc:
        assert "观测减少" in str(exc)
    else:
        raise AssertionError("观测行数不符时必须抛")

    # 隔离表没加上
    bad_q = dict(ok, semantic_observations_quarantine=19)
    try:
        mod._assert_balanced(before, bad_q, 10)
    except RuntimeError as exc:
        assert "隔离表增加" in str(exc)
    else:
        raise AssertionError("隔离行数不符时必须抛")

    # 修订表被动过
    bad_rev = dict(ok, observation_revisions=6)
    try:
        mod._assert_balanced(before, bad_rev, 10)
    except RuntimeError as exc:
        assert "observation_revisions" in str(exc)
    else:
        raise AssertionError("修订表变化时必须抛")


def test_exemption_check_precedes_the_not_re_fetchable_refusal():
    """豁免判断必须在 re_fetchable 硬闸**之前**——两者都是"不淘汰"，但文案不同。

    2026-11-12 实测：`akshare/aum` 声明 re_fetchable=false 且持有一条有效豁免，
    工具先命中硬闸，于是报"需人工改契约"——把人引去改一条本来正确的契约。
    正确的说法是"豁免期内不淘汰，责任人 X，到期 Y"。
    """
    src = SCRIPT.read_text()
    exemption_at = src.index('if state == "active":')
    refusal_at = src.index("不可回收层")
    assert exemption_at < refusal_at, \
        "豁免分支必须在 re_fetchable=false 拒绝之前，否则有效豁免会被报成'需改契约'"

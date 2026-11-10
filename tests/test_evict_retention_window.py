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

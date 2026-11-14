"""`scripts/check_fd_scripts_configmap.py` 的判据测试。

这条闸管的是「`executor=direct` 策略的脚本来自集群 ConfigMap，而那份副本没有出处」：
`_read_script` 是镜像优先，所以想用 ConfigMap 那份只能换名字（`_v2`），于是**生产日更
策略的唯一代码来源是一份没人比对的 ConfigMap**。2026-11-15 实测它与 `origin/main`
逐字节相同（sha `9581c0dd…`），所以这道闸是回归守卫：此后谁改了一边而不改另一边，
它要能红。
"""
from __future__ import annotations

import importlib.util
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO, "scripts", "check_fd_scripts_configmap.py")


def _load():
    name = "fd_scripts_cm_gate"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


gate = _load()
MAP = {"a.py": "scripts/a.py", "b.py": "scripts/b.py"}


def test_identical_content_is_same():
    (r,) = gate.compare({"a.py": "x = 1\n"}, MAP, ["a.py"], {"scripts/a.py": "x = 1\n"})
    assert r["verdict"] == "same"


def test_one_changed_line_is_diverged_and_names_the_line():
    (r,) = gate.compare({"a.py": "x = 1\ny = 2\n"}, MAP, ["a.py"],
                        {"scripts/a.py": "x = 1\ny = 3\n"})
    assert r["verdict"] == "diverged"
    assert "第 2 行" in r["detail"]


def test_trailing_newline_only_is_not_drift():
    """内联副本的常态写法差异：只差尾部换行不算分叉，否则这道闸会天天假红。"""
    (r,) = gate.compare({"a.py": "x = 1"}, MAP, ["a.py"], {"scripts/a.py": "x = 1\n"})
    assert r["verdict"] == "same"


def test_missing_key_in_cluster_is_a_hit_not_a_skip():
    """策略引用了它而 ConfigMap 没有 ⇒ 启动就报 script not found，必须红。"""
    (r,) = gate.compare({}, MAP, ["a.py"], {"scripts/a.py": "x\n"})
    assert r["verdict"] == "missing_in_cluster"


def test_repo_missing_is_undetermined_not_same():
    """取不到仓里那份＝判不出。读成「一致」就是把缺数当成结论。"""
    (r,) = gate.compare({"a.py": "x\n"}, MAP, ["a.py"], {})
    assert r["verdict"] == "undetermined"


def test_undeclared_keys_are_not_guessed():
    """没在映射表里的键不猜（推断出来的映射会替错的改名背书）。"""
    out = gate.compare({"z.py": "x\n"}, MAP, ["a.py", "z.py"],
                       {"scripts/a.py": "x\n"})
    assert [r["key"] for r in out] == ["a.py"]


def test_length_mismatch_reports_both_sizes():
    (r,) = gate.compare({"a.py": "x\ny\n"}, MAP, ["a.py"], {"scripts/a.py": "x\n"})
    assert r["verdict"] == "diverged"
    assert "集群 4 B" in r["detail"] and "仓内 2 B" in r["detail"]


def test_self_test_passes():
    assert gate._self_test() == 0


def test_the_v2_alias_maps_to_the_base_script():
    """`_v2` 是绕镜像遮蔽的别名，真源就是非 v2 那个文件——这条映射一旦改错，
    闸会拿一个不存在的路径比出「判不出」，而现场看起来像「一致」。"""
    assert gate.DEFAULT_MAP["bulk_ingest_astock_daily_v2.py"] == "scripts/bulk_ingest_astock_daily.py"

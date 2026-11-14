"""`check_policy_script_resolution.resolve` 的判据自证。

这道闸答的是「这条策略**实际会跑哪一份字节**」。它必须能区分四件事，而这四件
在「只看 ConfigMap 与仓一致」的闸里长得一样：

1. `_v2` 别名只在 ConfigMap 一档 ⇒ 生效（镜像里根本没这个名字）；
2. 镜像档陈旧而仓里已新 ⇒ **跑的不是仓里那份**（这是 astock 2026-11-15 的真实形态：
   镜像 52336 B / 无 tencent，仓内 62864 B / 有 tencent）；
3. 低优先档里也放着同一文件但字节不同 ⇒ 改那一档**不会生效**（惰性副本，事实而非命中）；
4. 三档全缺 ⇒ 启动即 FileNotFoundError。

外加两条纪律：映射没声明 ⇒ 判不出（不猜）；仓里取不到 ⇒ 判不出（缺数不是一致）。
"""
import hashlib
import importlib.util
import os
import sys

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO, "scripts", "check_policy_script_resolution.py")


def _load():
    """`scripts/` 不是包，按同仓既有约定用 spec 加载（见 test_fd_scripts_configmap_gate）。"""
    name = "policy_script_resolution_gate"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


gate = _load()
DEFAULT_MAP = gate.DEFAULT_MAP
PROBE_SRC = gate.PROBE_SRC
resolve = gate.resolve


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode()).hexdigest()


def _facts(*, a_image="old", a_cm="new", a_v2=None, b_image="b", b_cm="b",
           c_image="c"):
    files = {
        "a.py": {
            **({"/app/scripts": {"bytes": len(a_image), "sha": _sha(a_image)}}
               if a_image is not None else {}),
            **({"/opt/fd-scripts": {"bytes": len(a_cm), "sha": _sha(a_cm)}}
               if a_cm is not None else {}),
        },
        "b.py": {
            **({"/app/scripts": {"bytes": len(b_image), "sha": _sha(b_image)}}
               if b_image is not None else {}),
            **({"/opt/fd-scripts": {"bytes": len(b_cm), "sha": _sha(b_cm)}}
               if b_cm is not None else {}),
        },
        "c.py": {"/app/scripts": {"bytes": len(c_image), "sha": _sha(c_image)}},
    }
    if a_v2 is not None:
        files["a_v2.py"] = {"/opt/fd-scripts": {"bytes": len(a_v2), "sha": _sha(a_v2)}}
    repo = {"scripts/a.py": _sha("new"), "scripts/b.py": _sha("b"),
            "scripts/c.py": _sha("c")}
    mapping = {"a": "scripts/a.py", "a_v2": "scripts/a.py",
               "b": "scripts/b.py", "c": "scripts/c.py"}
    return files, repo, mapping


def test_alias_that_only_exists_in_configmap_is_ok():
    """`_v2` 是绕开镜像遮蔽的唯一办法——它只该在 ConfigMap 一档出现，且算 ok。"""
    files, repo, mapping = _facts(a_v2="new")
    [r] = resolve([{"name": "p", "script": "a_v2", "enabled": True}], files, repo, mapping)
    assert r["verdict"] == "ok"
    assert r["detail"].startswith("effective=/opt/fd-scripts")


def test_stale_image_tier_is_a_hit_even_when_configmap_is_correct():
    """镜像赢而镜像陈旧 ⇒ 命中。ConfigMap 里那份再新也救不了（这正是遮蔽的杀伤力）。"""
    files, repo, mapping = _facts(a_image="old", a_cm="new")
    [r] = resolve([{"name": "p", "script": "a", "enabled": True}], files, repo, mapping)
    assert r["verdict"] == "stale_effective"
    assert "effective=/app/scripts" in r["detail"]
    assert "跑的不是仓里那份" in r["detail"]


def test_inert_lower_copy_is_reported_but_is_not_the_hit_reason():
    """低档惰性副本单列：它今天是「改了不生效」，但它本身不是命中原因。"""
    files, repo, mapping = _facts(a_image="old", a_cm="new")
    [r] = resolve([{"name": "p", "script": "a", "enabled": True}], files, repo, mapping)
    tiers = [i["tier"] for i in r["inert_lower_copy"]]
    assert tiers == ["/opt/fd-scripts"]


def test_identical_copies_report_no_inert_copy():
    """两档同字节：不是「惰性副本」，别把正常形态报成隐患。"""
    files, repo, mapping = _facts(b_image="b", b_cm="b")
    [r] = resolve([{"name": "p", "script": "b", "enabled": True}], files, repo, mapping)
    assert r["verdict"] == "ok"
    assert r["inert_lower_copy"] == []


def test_script_missing_from_every_tier():
    files, repo, mapping = _facts()
    [r] = resolve([{"name": "p", "script": "zzz", "enabled": True}], files, repo, mapping)
    assert r["verdict"] == "missing"
    assert "FileNotFoundError" in r["detail"]


def test_undeclared_script_is_undetermined_not_ok():
    """映射没声明就不猜——猜出来的映射会在改名当天指向不存在的文件。"""
    files, repo, mapping = _facts()
    [r] = resolve([{"name": "p", "script": "a", "enabled": True}], files,
                  {**repo, "scripts/a.py": None}, mapping)
    assert r["verdict"] == "undetermined"
    assert "仓里取不到" in r["detail"]


def test_unmapped_script_name_is_undetermined():
    """文件在、名字却在 DEFAULT_MAP 之外 ⇒ 判不出（不猜它对应仓内哪份）。

    注意与 `missing` 的区别：`missing` 是**三档都没有这个文件**（启动必炸），
    `undetermined` 是**文件在而不知道它该等于仓里哪一份**（可能对可能错）。
    把后者报成 `ok` 就等于替一个没人声明过的映射背书。
    """
    files, repo, mapping = _facts()
    files["brand_new.py"] = {"/app/scripts": {"bytes": 1, "sha": _sha("x")}}
    [r] = resolve([{"name": "p", "script": "brand_new", "enabled": True}], files, repo, mapping)
    assert r["verdict"] == "undetermined"
    assert "DEFAULT_MAP" in r["detail"]


def test_disabled_policy_still_gets_a_verdict_but_is_marked():
    """停用策略照样算结论（它随时可能被启用），但 main 里按 enabled 过滤命中。"""
    files, repo, mapping = _facts()
    [r] = resolve([{"name": "p", "script": "c", "enabled": False}], files, repo, mapping)
    assert r["verdict"] == "ok"
    assert r["enabled"] is False


def test_image_wins_over_configmap_regardless_of_bytes():
    """档序是判据的一部分：镜像先命中就是它，哪怕 ConfigMap 那份更新。"""
    files, repo, mapping = _facts(a_image="new", a_cm="new")
    [r] = resolve([{"name": "p", "script": "a", "enabled": True}], files, repo, mapping)
    assert r["detail"].startswith("effective=/app/scripts")


def test_probe_source_compiles_and_reads_the_three_tiers():
    """探针源码必须能编译，且搜索链与 reconciler._read_script 一致。"""
    compile(PROBE_SRC, "<probe>", "exec")
    assert "/app/scripts" in PROBE_SRC
    assert "/opt/fd-scripts" in PROBE_SRC
    assert "FD_SCRIPTS_DIR" in PROBE_SRC
    assert "executor='direct'" in PROBE_SRC


def test_alias_mapping_points_at_the_base_script():
    """`_v2` 的真源是非 v2 那个文件；映射错了闸会报「判不出」，而判不出很像「没事」。"""
    assert DEFAULT_MAP["bulk_ingest_astock_daily_v2"] == "scripts/bulk_ingest_astock_daily.py"
    assert DEFAULT_MAP["bulk_ingest_astock_daily"] == "scripts/bulk_ingest_astock_daily.py"


@pytest.mark.parametrize("script", sorted(DEFAULT_MAP))
def test_every_mapped_script_points_into_the_scripts_dir(script):
    assert DEFAULT_MAP[script].startswith("scripts/")


# --- 别名退役判据（第六档输出）---------------------------------------------
# 「`_v2` 什么时候能删」在原来的口径里**结构性看不见**：按策略 basename 取指纹，
# 别名策略只量得到 `_v2.py`，镜像里那份基名脚本从来没被读到过。2026-11-15 实测
# 就是这个形状——闸说 `✓ ok`（别名好用），但镜像档基名仍是 52336 B 旧字节，
# 谁按「ok」去退役，生产就静默回退。下面三条把三态钉住。

alias_retirement = gate.alias_retirement


def test_alias_not_retirable_while_image_still_shadows_the_base_name():
    """基名在镜像档且是旧字节 ⇒ 暂不可退役，并点名那一档与两个 sha。"""
    files, repo, mapping = _facts(a_image="old", a_cm="new", a_v2="new")
    results = resolve([{"name": "p", "script": "a_v2", "enabled": True}],
                      files, repo, mapping)
    [a] = alias_retirement(results, files, repo, mapping)
    assert a["retirable"] is False
    assert "/app/scripts" in a["detail"]
    assert "旧字节" in a["detail"]


def test_alias_retirable_once_the_base_name_is_current():
    """基名那档与仓内一致（或根本没有它，落到正确的 ConfigMap）⇒ 可退役。"""
    files, repo, mapping = _facts(a_image=None, a_cm="new", a_v2="new")
    results = resolve([{"name": "p", "script": "a_v2", "enabled": True}],
                      files, repo, mapping)
    [a] = alias_retirement(results, files, repo, mapping)
    assert a["retirable"] is True
    assert a["detail"].startswith("退役后 effective=/opt/fd-scripts")


def test_alias_retirement_missing_base_name_names_file_not_found():
    """三档都没有基名 ⇒ 退役后启动即炸；这条必须说清，别只说「不可退役」。"""
    files = {"a_v2.py": {"/opt/fd-scripts": {"bytes": 3, "sha": _sha("new")}}}
    repo = {"scripts/a.py": _sha("new")}
    mapping = {"a_v2": "scripts/a.py"}
    results = resolve([{"name": "p", "script": "a_v2", "enabled": True}],
                      files, repo, mapping)
    [a] = alias_retirement(results, files, repo, mapping)
    assert a["retirable"] is False
    assert "FileNotFoundError" in a["detail"]


def test_non_alias_policy_produces_no_retirement_entry():
    """普通策略没有「退役」这回事——别把每一项都报成待办，那会淹掉真待办。"""
    files, repo, mapping = _facts()
    results = resolve([{"name": "p", "script": "a", "enabled": True}],
                      files, repo, mapping)
    assert alias_retirement(results, files, repo, mapping) == []


def test_retirement_undetermined_when_repo_lacks_the_file():
    """仓里取不到 ⇒ 判不出（None），不猜「可以退役」。"""
    files, repo, mapping = _facts(a_image=None, a_cm="new", a_v2="new")
    results = resolve([{"name": "p", "script": "a_v2", "enabled": True}],
                      files, {**repo, "scripts/a.py": None}, mapping)
    [a] = alias_retirement(results, files, {**repo, "scripts/a.py": None}, mapping)
    assert a["retirable"] is None

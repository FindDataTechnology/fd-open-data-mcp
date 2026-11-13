"""seed 的收窄闸：`--overwrite` 不得静默收窄留存。

背景（2026-11-13 实测）：`seed_retention_contracts.py --overwrite` 会照
`audit_retention_contracts.DEFAULT_CONTRACTS` 重写 `retention_class` 与
`retention_window_days`。而那份矩阵是**第二份真源**，会与库漂移——当时矩阵里
`akshare/rating`、`akshare/return` 还写着 T/30（库里已改判 W/1826，判 T 会搬走
256,746 行），六条 W 契约还是 730 天（库里已按"保留最近五年"抬到 1826）。
也就是说跑一次 `--overwrite` 就能把两个人工决定一起回滚，且**没有任何报错**。

本文件钉的是那个纯函数与矩阵本身的两条性质：
1. `narrowing_changes()` 只把"更严"的方向报出来（窗口变短 / 类向 T 靠 / 窗口被抹掉）；
2. 矩阵里的 W 契约全部 ≥ 1826 天（用户裁决的地板），且 `rating`/`return` 是 W 不是 T。
"""

import importlib.util
import pathlib
import sys

SCRIPTS = pathlib.Path(__file__).resolve().parents[1] / "scripts"


def _load(name):
    """按路径加载 scripts/ 下的脚本。

    必须先塞进 ``sys.modules`` 再 ``exec_module``：脚本里用了 ``@dataclass``，而
    dataclasses 在解析注解时会回查 ``sys.modules[cls.__module__]``——不注册就
    ``AttributeError: 'NoneType' object has no attribute '__dict__'``。
    """
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_narrowing_is_flagged():
    seed = _load("seed_retention_contracts")
    cur = {("akshare", "rating"): ("W", 1826)}
    inc = {("akshare", "rating"): ("T", 30)}
    out = seed.narrowing_changes(cur, inc)
    assert out, "W/1826 -> T/30 必须被判为收窄"
    assert "更严" in out[0]


def test_shorter_window_is_flagged():
    seed = _load("seed_retention_contracts")
    out = seed.narrowing_changes({("polygon", "price"): ("W", 1826)},
                                 {("polygon", "price"): ("W", 730)})
    assert out and "更短" in out[0]


def test_widening_is_allowed():
    seed = _load("seed_retention_contracts")
    assert seed.narrowing_changes({("polygon", "price"): ("W", 730)},
                                  {("polygon", "price"): ("W", 1826)}) == []
    assert seed.narrowing_changes({("akshare", "return"): ("T", 30)},
                                  {("akshare", "return"): ("W", 1826)}) == []


def test_new_families_are_not_narrowing():
    seed = _load("seed_retention_contracts")
    assert seed.narrowing_changes({}, {("new-src", "fam"): ("T", 30)}) == []


def test_matrix_W_windows_honour_the_five_year_floor():
    """矩阵里的 W 契约窗口必须 ≥1826 天（用户裁决是地板，不是建议）。"""
    audit = _load("audit_retention_contracts")
    bad = [(s, f, win) for (s, f, cls, win, *_rest) in audit.DEFAULT_CONTRACTS
           if cls == "W" and (win is None or win < 1826)]
    assert not bad, f"这些 W 契约低于五年地板（或没窗口）：{bad}"


def test_matrix_keeps_rating_and_return_as_windowed_not_current_only():
    """`rating`/`return` 已由 T 改判 W（判 T 会搬走 256,746 行）。矩阵不许改回 T。"""
    audit = _load("audit_retention_contracts")
    by_key = {(s, f): cls for (s, f, cls, *_r) in audit.DEFAULT_CONTRACTS}
    assert by_key[("akshare", "rating")] == "W"
    assert by_key[("akshare", "return")] == "W"

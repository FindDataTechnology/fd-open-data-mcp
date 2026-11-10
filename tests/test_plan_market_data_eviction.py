"""plan_market_data_eviction.py 的契约：只读、不删、口径可复核。

这个脚本的用途是"给决策报数"，所以测试重点不是查询结果，而是**它绝不动数据**、
以及人类可读的输出不会把量级说错（78% 说成 7% 这类错误会直接导致错误决策）。
"""

import pathlib
import re

import pytest

SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "plan_market_data_eviction.py"


def load():
    import importlib.util
    spec = importlib.util.spec_from_file_location("plan_evict", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_script_never_executes_a_delete():
    """静态硬守卫：整个脚本里不许出现执行 DELETE 的调用。

    `--show-sql` 只打印语句是刻意的：删已定型观测与 R1 冲突，必须由 change 声明边界。
    """
    src = SCRIPT.read_text()
    # 任何 execute(...) 里带 DELETE / TRUNCATE 都算违约
    for m in re.finditer(r"execute\(\s*[^)]*", src):
        assert "DELETE" not in m.group(0).upper()
        assert "TRUNCATE" not in m.group(0).upper()
    assert 'print("DELETE FROM' in src, "--show-sql 必须是打印而不是执行"


def test_human_scales_are_right():
    mod = load()
    assert mod.human(0) == "0B"
    assert mod.human(512) == "512B"
    assert mod.human(1024) == "1KB"
    assert mod.human(1024 * 1024) == "1MB"
    assert mod.human(3 * 1024 ** 3) == "3.0GB"


def test_refetchable_sources_include_the_dominant_one():
    """实测 akshare 占 98.4% 的行——它必须在"可重取"名单里，否则报告口径就错了。"""
    mod = load()
    assert "akshare" in mod.REFETCHABLE_SOURCES
    assert "polygon" in mod.REFETCHABLE_SOURCES


def test_missing_dsn_is_a_usage_error(monkeypatch, capsys):
    mod = load()
    monkeypatch.delenv("FD_OPEN_DATA_MCP_DATABASE_URL", raising=False)
    assert mod.main([]) == 2
    assert "缺少 --db-url" in capsys.readouterr().err

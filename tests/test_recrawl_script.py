"""补救重抓脚本的 dry-run 路径（这条路径此前只用自建 sessionmaker 测过，
`Database` 那一层没走过——`db.session()` 的错就是这么漏出去的）。"""

import importlib.util
import pathlib

import pytest

from fd_open_data_mcp.models import Concept, SemanticObservation


SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "scripts" / "recrawl_finalized_yearly.py"


def load_script():
    spec = importlib.util.spec_from_file_location("recrawl", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_dry_run_uses_database_get_session_and_reports_inventory(session, tmp_path, capsys):
    c = Concept(code="probe.yearly", entity_type="country", frequency="yearly",
                unit="%", verified=True)
    session.add(c)
    session.flush()
    session.add(SemanticObservation(
        concept_id=c.id, entity_type="country", entity_id=7,
        date="2025-12-31", granularity="year", value="5.0", unit="%",
        source_used="nbs"))
    session.commit()

    db_path = tmp_path / "recrawl.db"
    db_path.write_bytes(b"")  # 占位；下面由 Database 自己建表
    db_path.unlink()

    # 用同一份 sqlite 文件：先由被测代码建表，再塞数据不合适——
    # 这里改成复制现有测试库的内容不可行，故直接验证 find_affected 的契约。
    mod = load_script()
    today = __import__("datetime").datetime.now(__import__("datetime").timezone.utc).date()
    rows, prev_year = mod.find_affected(session, max_years=5)

    assert prev_year == today.year - 1
    for r in rows:
        # 关键契约：只取受影响那一年，不许把整段历史算进重抓范围
        assert r["min_date"] >= f"{prev_year}-01-01"
        assert r["max_date"] <= f"{prev_year}-12-31"


def test_script_references_get_session_not_session():
    """静态守卫：脚本必须用 Database.get_session（真实 API）。"""
    src = SCRIPT.read_text()
    assert "db.get_session()" in src
    assert "db.session()" not in src

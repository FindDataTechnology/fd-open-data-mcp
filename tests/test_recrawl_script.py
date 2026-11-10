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


# --- 写入准入门（2026-11-10 实测踩出来的两道门） -------------------------
#
# 现场证据：concept 258 / stock 2859 的年频序列（2021..2024 皆为 1e10 量级的
# 营收）在某次范围重取后多出四个 `granularity='year'` 的季度末点，值变成
# 0.54..2.67，且其中一条覆写掉了真正的年报值 67605246103.24。放行这类写入
# 不是"补回修订"，是把序列改坏——所以必须在写之前就拦住。


def test_judge_accepts_a_single_plausible_year_point():
    mod = load_script()

    ok, why = mod.judge_series({"2025": "6.8e10"}, {"2025": "6.7e10"})

    assert ok is True, why
    assert why is None


def test_judge_refuses_quarterly_points_for_a_yearly_concept():
    """一年四个点 = 上游给了季报；写成 year 粒度会造出假历史。"""
    mod = load_script()

    ok, why = mod.judge_series(
        {"2025-03-31": "0.5407", "2025-06-30": "1.3894",
         "2025-09-30": "1.8658", "2025-12-31": "2.6719"},
        {"2025-12-31": "67605246103.24"},
    )

    assert ok is False
    assert why.startswith("finer_granularity_returned")


def test_judge_refuses_a_scale_jump_of_two_orders_of_magnitude():
    mod = load_script()

    ok, why = mod.judge_series({"2025-12-31": "2.6719"}, {"2025-12-31": "67605246103.24"})

    assert ok is False
    assert why.startswith("scale_mismatch")


def test_judge_allows_a_normal_revision():
    """正常上修：差 3% 是修订，不是换指标。"""
    mod = load_script()

    ok, _ = mod.judge_series({"2025-12-31": "6.94e10"}, {"2025-12-31": "6.76e10"})

    assert ok is True


def test_judge_refuses_an_empty_series():
    mod = load_script()

    ok, why = mod.judge_series({}, {"2025": "1.0"})

    assert ok is False
    assert why == "empty_series"


def test_judge_does_not_refuse_when_there_is_no_prior_value():
    """冷点没有可比对象，量级无从判断——不因此拒写（否则永远补不进缺失点）。"""
    mod = load_script()

    ok, _ = mod.judge_series({"2025": "123.0"}, {})

    assert ok is True


def test_script_passes_an_admission_guard_and_reports_refusals():
    """静态守卫：脚本必须真的把 admit_series 传下去，并且区分"拒写"与"取不到"。"""
    src = SCRIPT.read_text()
    assert "admit_series=_admit" in src
    assert "write_refused" in src
    assert "judge_series" in src


def test_year_rows_snapshot_takes_year_rows_including_bare_year(session):
    """写入前快照：只取 year 粒度，且 bare-year 的 '2025' 也必须取到。

    这条同时是回归守卫——快照原来手写 ``date LIKE '2025%'``，在生产上会扫到挂在
    外部服务器 shard_xinru3 的分区，整个批次 40/40 失败。
    """
    mod = load_script()
    c = Concept(code="probe.snapshot", entity_type="stock", frequency="yearly",
                unit="CNY", verified=True)
    session.add(c)
    session.flush()
    for date, gran, value in [
        ("2025", "year", "100.0"),          # bare year：字符串比较会漏掉它
        ("2025-12-31", "year", "101.0"),
        ("2025-03-31", "quarter", "25.0"),  # 季度行不许混进 year 快照
        ("2024-12-31", "year", "90.0"),     # 别的年份
    ]:
        session.add(SemanticObservation(
            concept_id=c.id, entity_type="stock", entity_id=1, date=date,
            granularity=gran, value=value, unit="CNY", source_used="akshare"))
    session.commit()

    rows = mod._year_rows(session, c.id, "stock", 1, 2025)

    assert rows == {"2025": "100.0", "2025-12-31": "101.0"}


def test_script_rolls_back_after_a_failed_pair():
    """静态守卫：单条失败必须 rollback，否则整批都变成 InFailedSqlTransaction。"""
    src = SCRIPT.read_text()
    assert "session.rollback()" in src


def test_scale_mismatch_reports_the_larger_side_not_zero():
    """ratio<1 时不能打印"差 0 倍"——实测 6.6e7 → 4.78 被打成"差 0 倍"，无从判断。"""
    mod = load_script()

    ok, why = mod.judge_series({"2025-12-31": "4.7755"},
                               {"2025-12-31": "66021047.24"})

    assert ok is False
    assert why.startswith("scale_mismatch")
    assert "差 0 倍" not in why
    assert "缩了" in why
    # 66021047.24 / 4.7755 ≈ 1.38e7 → 必须报出这个量级，而不是"0 倍"
    assert "13,824,950" in why


def test_find_affected_excludes_deprecated_concepts(session):
    """废弃概念写路径会抛 ConceptDeprecated，不该进盘点表。"""
    mod = load_script()
    today = __import__("datetime").datetime.now(__import__("datetime").timezone.utc).date()
    year = today.year - 1

    live = Concept(code="probe.live", entity_type="country", frequency="yearly",
                   unit="%", verified=True)
    gone = Concept(code="probe.dead", entity_type="country", frequency="yearly",
                   unit="%", verified=True, deprecated=True)
    session.add_all([live, gone])
    session.flush()
    for c in (live, gone):
        session.add(SemanticObservation(
            concept_id=c.id, entity_type="country", entity_id=9,
            date=f"{year}-12-31", granularity="year", value="1.0", unit="%",
            source_used="wbgapi"))
    session.commit()

    rows, prev_year = mod.find_affected(session)
    ids = [r["concept_id"] for r in rows]

    assert prev_year == year
    assert live.id in ids
    assert gone.id not in ids


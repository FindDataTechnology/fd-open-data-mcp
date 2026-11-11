"""T 类（只留当期）写语义：写完之后只该剩一条。

这是第 3 期的核心：此前契约写着"只留当期"，但写路径按日追加，于是
`tenure_days` 这类派生快照近 7 天能长到每实体 47 行，清理过后几个月又回来。
审计器能用 `T_class_accruing_history` 抓出它，但只有写路径真的执行契约才算修好。

四条判据：
  1. T 类连写三天 → 只剩最新一条（不是三条）；
  2. 旧的几条被并掉时有 batch 修订留痕（不是静默消失）；
  3. 非 T 类（F/W）行为完全不变——这是最要紧的一条，改写入路径最容易误伤别人；
  4. 没有契约的族也不变（"不知道"不等于"按 T 处理"）。
"""

import pytest

from fd_open_data_mcp.fetch.cache import write_cache_range
from fd_open_data_mcp.models import (
    Concept,
    DataRetentionContract,
    ObservationRevision,
    SemanticObservation,
)

ENT = ("stock", 1)


def _concept(session, code="tenure_days"):
    c = Concept(code=code, entity_type="stock", frequency="daily",
                unit="day", verified=True)
    session.add(c)
    session.commit()
    return c


def _contract(session, source, family, cls, window_days=None):
    session.add(DataRetentionContract(
        source=source, family=family, retention_class=cls,
        retention_window_days=window_days, re_fetchable=True,
        upstream_history_mutable=False, volume_budget_mb=64))
    session.commit()


def _rows(session, cid):
    return (session.query(SemanticObservation)
            .filter_by(concept_id=cid, entity_type=ENT[0], entity_id=ENT[1])
            .order_by(SemanticObservation.date).all())


def test_t_class_keeps_only_latest_snapshot(session):
    c = _concept(session)
    _contract(session, "akshare", "tenure_days", "T", 30)

    for d in ("2026-11-01", "2026-11-02", "2026-11-03"):
        write_cache_range(session, c.id, ENT[0], ENT[1],
                          {d: 100}, "day", "akshare", granularity="day")

    rows = _rows(session, c.id)
    assert len(rows) == 1, f"T 类应只留一条，实际 {len(rows)} 条"
    assert rows[0].date == "2026-11-03"


def test_consolidation_leaves_a_batch_revision(session):
    c = _concept(session)
    _contract(session, "akshare", "tenure_days", "T", 30)
    for d in ("2026-11-01", "2026-11-02"):
        write_cache_range(session, c.id, ENT[0], ENT[1], {d: 100}, "day", "akshare",
                          granularity="day")

    revs = (session.query(ObservationRevision)
            .filter_by(concept_id=c.id, revision_kind="batch").all())
    assert revs, "并掉旧快照必须留一条 batch 修订"
    assert all(r.batch_id and r.batch_id.startswith("snapshot-consolidate-")
               for r in revs)
    assert sum(r.point_count or 0 for r in revs) >= 1


def test_non_t_classes_are_untouched(session):
    """F/W 行为必须完全不变——改写入路径最容易误伤的正是它们。"""
    for cls in ("F", "W"):
        c = _concept(session, code=f"price_{cls.lower()}")
        _contract(session, "akshare", f"price_{cls.lower()}", cls, 730)
        for d in ("2026-11-01", "2026-11-02", "2026-11-03"):
            write_cache_range(session, c.id, ENT[0], ENT[1], {d: 1.0}, "CNY",
                              "akshare", granularity="day")
        assert len(_rows(session, c.id)) == 3, f"{cls} 类不应被合并"


def test_no_contract_means_no_consolidation(session):
    """没有契约的族不按 T 处理：不知道不等于可以删。"""
    c = _concept(session, code="no_contract_family")
    for d in ("2026-11-01", "2026-11-02", "2026-11-03"):
        write_cache_range(session, c.id, ENT[0], ENT[1], {d: 1.0}, "CNY",
                          "unknown-src", granularity="day")
    assert len(_rows(session, c.id)) == 3


def test_single_point_writer_also_consolidates(session):
    """单点写入（fetch 路径）也必须执行 T 语义，否则留一条后门。"""
    from fd_open_data_mcp.fetch.cache import write_cache

    c = _concept(session)
    _contract(session, "akshare", "tenure_days", "T", 30)
    for d in ("2026-11-01", "2026-11-02", "2026-11-03"):
        write_cache(session, c.id, ENT[0], ENT[1], d, "100", "day", "akshare",
                    granularity="day")

    rows = _rows(session, c.id)
    assert len(rows) == 1
    assert rows[0].date == "2026-11-03"


def test_insert_only_path_also_consolidates(session):
    """补数路径（new-only）也必须执行 T 语义，否则它就是绕过 T 的后门。

    2026-11-11 实测：T 语义上线后 `nav` 仍从 1,236 涨到 1,928 行（1.56 行/实体），
    涨的正是从 `write_cache_range_new_only` 补进来的点——这条路径当时既不过闸门、
    也不裁 W 窗口、更不做 T 合并。
    """
    from fd_open_data_mcp.fetch.cache import write_cache_range_new_only

    c = _concept(session, code="tenure_days_new_only")
    _contract(session, "akshare", "tenure_days_new_only", "T", 30)
    write_cache_range(session, c.id, ENT[0], ENT[1], {"2026-11-01": 1}, "day",
                      "akshare", granularity="day")
    # 补数补进两个更早的点 → 合并后仍只留最新那条
    write_cache_range_new_only(session, c.id, ENT[0], ENT[1],
                               {"2026-10-01": 2, "2026-10-15": 3}, "day",
                               "akshare", granularity="day")
    rows = _rows(session, c.id)
    assert len(rows) == 1, f"补数后应仍只留一条，实际 {len(rows)} 条"
    assert rows[0].date == "2026-11-01"

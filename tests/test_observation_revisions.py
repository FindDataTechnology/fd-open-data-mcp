"""修订留痕（define-ingest-revision-and-coverage）。

要害：覆写不得让旧值消失。这条比"看起来对"难——`write_cache_range` 一直是
原地 upsert，而 `_period_final` 已经保证定型后的事实永不重取。于是同一份数据
有两种待遇：老的不许改，新的改了没痕迹。
"""
from __future__ import annotations

import pytest

from fd_open_data_mcp.fetch.cache import (
    write_cache_range,
    write_cache_range_new_only,
)
from fd_open_data_mcp.models import Concept, ObservationRevision, SemanticObservation

T, EID, SRC = "country", 7, "akshare"


@pytest.fixture
def cid(session) -> int:
    """观测带 concept 外键，裸 id 会撞 FK —— 建一行真 concept。"""
    c = Concept(code="gdp.total", entity_type=T, frequency="yearly",
                unit="USD", verified=True)
    session.add(c)
    session.commit()
    return c.id


def _counts(session):
    return (
        session.query(SemanticObservation).count(),
        session.query(ObservationRevision).count(),
    )


def _write(session, cid, rows, *, source=SRC, **kw):
    return write_cache_range(session, cid, T, EID, rows, "USD",
                             source, granularity="day", **kw)


def test_first_write_creates_no_revision(session, cid):
    """首次写入不是修订：没有旧值，就不该凭空产生修订记录。"""
    _write(session, cid, {"2026-11-01": 1.0, "2026-11-02": 2.0})
    assert _counts(session) == (2, 0)


def test_overwrite_keeps_old_value(session, cid):
    """核心用例：定型前覆写必须留下旧值、新值与来源。"""
    _write(session, cid, {"2026-11-08": 10.0})
    _write(session, cid, {"2026-11-08": 12.5}, revision_reason="上游修订")

    obs = session.query(SemanticObservation).one()
    assert obs.value == "12.5", "当前值应是新值"

    rev = session.query(ObservationRevision).one()
    assert rev.revision_kind == "overwrite"
    assert rev.old_value == "10.0", "旧值必须可查——这正是先前缺失的那一半"
    assert rev.new_value == "12.5"
    assert rev.reason == "上游修订"
    assert (rev.concept_id, rev.entity_type, rev.entity_id, rev.date) == (cid, T, EID, "2026-11-08")


def test_repeated_overwrites_accumulate_history(session, cid):
    """改两次就要有两条记录：只留最后一版等于没有历史。"""
    _write(session, cid, {"2026-11-08": 1.0})
    _write(session, cid, {"2026-11-08": 2.0})
    _write(session, cid, {"2026-11-08": 3.0})

    olds = [r.old_value for r in
            session.query(ObservationRevision).order_by(ObservationRevision.id)]
    assert olds == ["1.0", "2.0"], "每次覆写前的值按顺序都在"
    assert session.query(SemanticObservation).one().value == "3.0"


def test_insert_only_path_never_revises(session, cid):
    """补数路径本来就不覆写，因此不该产生修订记录。"""
    _write(session, cid, {"2026-11-01": 5.0})
    write_cache_range_new_only(session, cid, T, EID,
                               {"2026-11-01": 9.0, "2026-11-02": 6.0},
                               "USD", SRC, granularity="day")
    assert [r.old_value for r in session.query(ObservationRevision)] == []
    values = {o.date: o.value for o in session.query(SemanticObservation)}
    assert values == {"2026-11-01": "5.0", "2026-11-02": "6.0"}


def test_batch_revision_is_one_row_not_per_point(session, cid):
    """前复权整段重写：记一个批次（范围 + 点数 + 原因），不逐点留痕。"""
    rows = {f"2026-10-{d:02d}": float(d) for d in range(1, 11)}
    _write(session, cid, rows)
    _write(session, cid, {k: v + 100 for k, v in rows.items()},
           revision_batch_id="div-2026-11-09-600519", revision_reason="分红后前复权整体重写")

    revs = session.query(ObservationRevision).all()
    assert len(revs) == 1, "整段修订只记一行"
    rev = revs[0]
    assert rev.revision_kind == "batch"
    assert rev.batch_id == "div-2026-11-09-600519"
    assert rev.point_count == 10
    assert (rev.range_start, rev.range_end) == ("2026-10-01", "2026-10-10")
    assert "分红" in (rev.reason or "")


def test_batch_with_no_existing_points_writes_no_revision(session, cid):
    """整段都是新点（首次抓取）时不是修订，批次行也不该出现。"""
    _write(session, cid, {"2026-10-01": 1.0}, revision_batch_id="div-x")
    assert session.query(ObservationRevision).count() == 0


def test_revision_is_per_source(session, cid):
    """两个来源对同一点各自修订：修订记录按 (date, source) 分开，不互相串。"""
    _write(session, cid, {"2026-11-08": 1.0}, source="akshare")
    _write(session, cid, {"2026-11-08": 2.0}, source="eia")
    _write(session, cid, {"2026-11-08": 1.5}, source="akshare")

    revs = session.query(ObservationRevision).all()
    assert len(revs) == 1, "只有 akshare 那一行被修订过"
    assert revs[0].source_used == "akshare"
    assert revs[0].old_value == "1.0"

"""修订保留期清理（`fetch/revision_retention.py`）。

守住四条：
1. **未配置 = 不删**（没有默认 TTL，"忘了配"不等于"开始删历史"）；
2. 默认 dry-run，只统计；
3. `batch` 行默认受保护——它是"这段不可逐点回放"的唯一证据；
4. 真删时按批提交，且只删早于 cut-off 的行。
"""

from datetime import datetime, timedelta

import pytest

from fd_open_data_mcp.fetch.revision_retention import prune_revisions
from fd_open_data_mcp.models import Concept, ObservationRevision

NOW = datetime(2026, 11, 10, 12, 0)


@pytest.fixture
def cid(session):
    c = Concept(code="retention.demo", entity_type="country",
                frequency="yearly", unit="%", verified=True)
    session.add(c)
    session.commit()
    return c.id


def add_rev(session, cid, *, days_ago, kind="overwrite", date="2020-12-31", bid=None):
    session.add(ObservationRevision(
        concept_id=cid, entity_type="country", entity_id=1, date=date,
        granularity="year", source_used="probe", revision_kind=kind,
        old_value="1", new_value="2", batch_id=bid,
        range_start=date if kind == "batch" else None,
        range_end=date if kind == "batch" else None,
        point_count=1 if kind == "batch" else None,
        revised_at=NOW - timedelta(days=days_ago),
    ))
    session.commit()


def test_unconfigured_means_no_cleanup(session, cid):
    """不传 older_than_days → 一行都不动。这是最重要的默认值。"""
    add_rev(session, cid, days_ago=5000)

    out = prune_revisions(session, dry_run=False, now=NOW)

    assert out["enabled"] is False
    assert out["deleted"] == 0
    assert session.query(ObservationRevision).count() == 1


def test_dry_run_reports_but_deletes_nothing(session, cid):
    add_rev(session, cid, days_ago=400)
    add_rev(session, cid, days_ago=10)

    out = prune_revisions(session, older_than_days=365, now=NOW)

    assert out["dry_run"] is True
    assert out["candidates"]["overwrite"] == 1
    assert out["deleted"] == 0
    assert session.query(ObservationRevision).count() == 2


def test_batch_rows_are_protected_by_default(session, cid):
    """batch 行是「不可逐点回放」的唯一证据，默认不删。"""
    add_rev(session, cid, days_ago=400, kind="batch", bid="adj-1")

    out = prune_revisions(session, older_than_days=365, dry_run=False, now=NOW)

    assert out["deleted"] == 0
    assert out["kept_batch"] == 1
    assert session.query(ObservationRevision).count() == 1


def test_include_batch_must_be_explicit(session, cid):
    add_rev(session, cid, days_ago=400, kind="batch", bid="adj-2")

    out = prune_revisions(session, older_than_days=365, include_batch=True,
                          dry_run=False, now=NOW)

    assert out["deleted"] == 1
    assert session.query(ObservationRevision).count() == 0


def test_only_rows_older_than_cutoff_are_deleted(session, cid):
    add_rev(session, cid, days_ago=400, date="2019-12-31")
    add_rev(session, cid, days_ago=10, date="2025-12-31")

    out = prune_revisions(session, older_than_days=365, dry_run=False, now=NOW)

    assert out["deleted"] == 1
    left = session.query(ObservationRevision).all()
    assert [r.date for r in left] == ["2025-12-31"]


def test_batched_deletion_covers_everything(session, cid):
    for i in range(7):
        add_rev(session, cid, days_ago=400 + i, date=f"2019-12-{10+i:02d}")

    out = prune_revisions(session, older_than_days=365, dry_run=False,
                          batch_size=2, now=NOW)

    assert out["deleted"] == 7
    assert session.query(ObservationRevision).count() == 0


def test_negative_window_rejected(session, cid):
    with pytest.raises(ValueError):
        prune_revisions(session, older_than_days=-1, now=NOW)


def test_cutoff_is_reported(session, cid):
    add_rev(session, cid, days_ago=400)

    out = prune_revisions(session, older_than_days=30, now=NOW)

    assert out["cutoff"].startswith("2026-10-11")


# --- 粒度作用域：全局 TTL 是错的形状 ----------------------------------------

def add_rev_gran(session, cid, *, days_ago, granularity, date):
    from datetime import datetime, timedelta
    session.add(ObservationRevision(
        concept_id=cid, entity_type="country", entity_id=1, date=date,
        granularity=granularity, source_used="probe", revision_kind="overwrite",
        old_value="1", new_value="2",
        revised_at=NOW - timedelta(days=days_ago),
    ))
    session.commit()


def test_granularity_scope_protects_annual_revisions(session, cid):
    """只清日频：年度修订必须原样留着。

    日频产出绝大多数修订行，但 as-of 回放价值最低；年/月频行少而历史价值高。
    全局 TTL 会为了压日频的量而删掉年度修订——拿贵的换便宜的。
    """
    add_rev_gran(session, cid, days_ago=900, granularity="day", date="2024-05-06")
    add_rev_gran(session, cid, days_ago=900, granularity="year", date="2024-12-31")

    out = prune_revisions(session, older_than_days=365, granularities=("day",),
                          dry_run=False, now=NOW)

    assert out["deleted"] == 1
    left = session.query(ObservationRevision).all()
    assert [r.granularity for r in left] == ["year"], "年度修订不得被日频策略带走"


def test_granularity_scope_applies_to_counting_too(session, cid):
    """dry-run 的候选计数也要按粒度收窄，否则"看清会删多少"是假的。"""
    add_rev_gran(session, cid, days_ago=900, granularity="day", date="2024-05-06")
    add_rev_gran(session, cid, days_ago=900, granularity="year", date="2024-12-31")

    out = prune_revisions(session, older_than_days=365, granularities=("day",),
                          now=NOW)

    assert out["candidates"]["overwrite"] == 1
    assert out["deleted"] == 0
    assert session.query(ObservationRevision).count() == 2

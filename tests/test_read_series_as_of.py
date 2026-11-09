"""`server.read_series` 的 as_of_vintage 分支（任务 4.5 的服务层验证）。

模块层（`read_range_as_of`）已经测过；这里补的是**工具入口那条分支**——
它在 `server.py` 里，若不打这条路径，分支写错了也只有上产才会知道。

两条必须成立：
1. 不传 `as_of_vintage` 时行为与改动前一致（零影响）；
2. 传了之后不可回放的点**不返回数值**，并带 `vintage` 自证 + `note`。
"""

from datetime import datetime

import pytest

from fd_open_data_mcp.fetch.cache import write_cache
from fd_open_data_mcp.models import Concept, ObservationRevision

T, E = "country", 42


@pytest.fixture
def concept(session):
    c = Concept(code="vintage_demo", entity_type=T, frequency="yearly",
                unit="%", verified=True)
    session.add(c)
    session.commit()
    return c.id


def _call(concept_id, **overrides):
    from fd_open_data_mcp.server import read_series

    args = dict(concept_id=concept_id, entity_type=T, entity_id=E,
                start="2018-01-01", end="2022-12-31")
    args.update(overrides)
    return read_series(**args)


def add_revision(session, cid, date, old, new, revised_at):
    session.add(ObservationRevision(
        concept_id=cid, entity_type=T, entity_id=E, date=date, granularity="year",
        source_used="worldbank", revision_kind="overwrite",
        old_value=old, new_value=new, revised_at=revised_at,
    ))
    session.commit()


def test_without_as_of_behaves_as_before(session, concept):
    """零影响：不传 as_of_vintage 时返回体里没有 vintage 键。"""
    write_cache(session, concept, T, E, "2020-12-31", "5.0", "%", "worldbank",
                granularity="year")

    out = _call(concept)

    assert out["count"] == 1
    assert "vintage" not in out
    assert "as_of_vintage" not in out


def test_as_of_branch_replays_historical_value(session, concept):
    write_cache(session, concept, T, E, "2020-12-31", "5.4", "%", "worldbank",
                granularity="year")
    add_revision(session, concept, "2020-12-31", "5.2", "5.4",
                 datetime(2024, 6, 1))

    out = _call(concept, as_of_vintage="2024-01-01")

    assert out["count"] == 1
    assert out["points"][0]["value"] == "5.2", "应回放到修订前的值"
    assert out["points"][0]["vintage_status"] == "exact"
    assert out["vintage"]["fully_exact"] is True
    assert out["as_of_vintage"].startswith("2024-01-01")


def test_as_of_branch_omits_unreplayable_points_and_notes_it(session, concept):
    """batch 覆盖区间 → 不返回数值 + note 说明 + vintage_details 带 batch_id。"""
    write_cache(session, concept, T, E, "2020-12-31", "999", "%", "worldbank",
                granularity="year")
    session.add(ObservationRevision(
        concept_id=concept, entity_type=T, entity_id=E, date="2020-01-01",
        granularity="year", source_used="worldbank", revision_kind="batch",
        batch_id="adj-1", range_start="2018-01-01", range_end="2022-12-31",
        revised_at=datetime(2024, 5, 1),
    ))
    session.commit()

    out = _call(concept, as_of_vintage="2024-06-01")

    assert out["count"] == 0, "不可回放的点不得返回数值"
    assert out["vintage"]["batch_unreplayable"] == 1
    assert "历史回放不完整" in out["note"]
    assert out["vintage_details"][0]["batch_id"] == "adj-1"


def test_as_of_branch_never_dispatches_upstream(session, concept, monkeypatch):
    """as-of 读只重放已落库的修订，绝不回源。"""
    import fd_open_data_mcp.fetch.dispatch as dispatch_mod

    def _boom(*a, **kw):  # pragma: no cover - 不应被调用
        raise AssertionError("as-of 读不得回源")

    monkeypatch.setattr(dispatch_mod, "instrumented_fetch", _boom)
    write_cache(session, concept, T, E, "2020-12-31", "5.0", "%", "worldbank",
                granularity="year")

    # 请求时点须晚于该行的写入时刻（fetched_at=now），否则正确答案是 absent
    out = _call(concept, as_of_vintage="2099-01-01")

    assert out["count"] == 1


def test_absent_point_is_not_served_as_current_value(session, concept):
    """首次写入晚于请求时点 → absent，且不返回数值。"""
    write_cache(session, concept, T, E, "2020-12-31", "5.0", "%", "worldbank",
                granularity="year")
    # write_cache 把 fetched_at 设为 now；请求时点放到 2019 年 → 该点当时不存在
    out = _call(concept, as_of_vintage="2019-06-01")

    assert out["count"] == 0
    assert out["vintage"]["absent"] == 1

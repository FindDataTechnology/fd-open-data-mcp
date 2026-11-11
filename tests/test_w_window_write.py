"""W 类（滚动窗口）写语义：窗口外的点必须在**入口**被丢弃。

背景（2026-11-11 实测）：把 `aum` 超窗行清进隔离表之后，一次全量重抓又把 234 条
2021 年的点写了回来——W 类的写路径此前不做任何窗口裁剪，于是"清理"只是暂时的。
T 类已有 `_snapshot_consolidate` 管"只留当期"，W 类缺的是另一半：**别让窗外的点进来**。

四条判据：
  1. W 类：窗外点不入库，窗内点照常写；
  2. 全被丢弃时返回 0（不要写空的 rollup / 空的 batch 修订）；
  3. F / P 类完全不受影响（反向守卫：改热路径最容易误伤的就是它们）；
  4. 裸年、`YYYY-MM` 这类没有唯一含义的日期**不猜**，一律保留。
"""

from datetime import datetime, timedelta, timezone

from fd_open_data_mcp.fetch.cache import write_cache_range
from fd_open_data_mcp.models import (
    Concept,
    DataRetentionContract,
    SemanticObservation,
)

ENT = ("stock", 1)


def _concept(session, code="aum_window"):
    c = Concept(code=code, entity_type="stock", frequency="daily",
                unit="CNY", verified=True)
    session.add(c)
    session.commit()
    return c


def _contract(session, source, family, cls, window_days=None):
    session.add(DataRetentionContract(
        source=source, family=family, retention_class=cls,
        retention_window_days=window_days, re_fetchable=True,
        upstream_history_mutable=True, volume_budget_mb=64))
    session.commit()


def _dates(session, cid):
    return sorted(r.date for r in session.query(SemanticObservation)
                  .filter_by(concept_id=cid, entity_type=ENT[0], entity_id=ENT[1]))


def _day(offset: int) -> str:
    return (datetime.now(timezone.utc).date() + timedelta(days=offset)).isoformat()


def test_w_class_drops_points_outside_window(session):
    c = _concept(session)
    _contract(session, "akshare", "aum_window", "W", 30)
    write_cache_range(session, c.id, ENT[0], ENT[1],
                      {_day(-100): 1.0, _day(-1): 2.0, _day(0): 3.0},
                      "CNY", "akshare", granularity="day")
    assert _dates(session, c.id) == [_day(-1), _day(0)]


def test_all_points_dropped_returns_zero(session):
    c = _concept(session)
    _contract(session, "akshare", "aum_window", "W", 30)
    n = write_cache_range(session, c.id, ENT[0], ENT[1],
                          {_day(-100): 1.0, _day(-90): 2.0},
                          "CNY", "akshare", granularity="day")
    assert n == 0
    assert _dates(session, c.id) == []


def test_f_and_p_classes_are_untouched(session):
    """F/P 没有窗口语义：老点必须照常写入（F 是全历史，P 是不可回收）。"""
    for cls in ("F", "P"):
        c = _concept(session, code=f"keep_{cls.lower()}")
        _contract(session, "akshare", f"keep_{cls.lower()}", cls, None)
        write_cache_range(session, c.id, ENT[0], ENT[1],
                          {_day(-3000): 1.0, _day(0): 2.0},
                          "CNY", "akshare", granularity="day")
        assert len(_dates(session, c.id)) == 2, f"{cls} 类不该被窗口裁剪"


def test_ambiguous_date_formats_are_kept_not_guessed(session):
    """裸年 `2019` 与 `2019-06` 与窗口下界比较没有唯一含义——不猜，保留。"""
    c = _concept(session)
    _contract(session, "akshare", "aum_window", "W", 30)
    write_cache_range(session, c.id, ENT[0], ENT[1],
                      {"2019": 1.0, "2019-06": 2.0, _day(0): 3.0},
                      "CNY", "akshare", granularity="day")
    assert _dates(session, c.id) == ["2019", "2019-06", _day(0)]

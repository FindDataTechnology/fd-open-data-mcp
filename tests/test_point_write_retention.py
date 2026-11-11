"""单点写入路径（`write_cache`）也必须执行留存语义。

为什么单独测这一条：三个写入口里，`write_cache_range` 与
`write_cache_range_new_only` 早就有"契约闸 + 窗口裁剪 + T 合并"三件套，而
`write_cache`（`read`/`fetch(点)` 的活路径）只做了 T 合并——2026-11-11 在**线上镜像**
里实测确认（`gate=False window=False`）。缺口的形状是最难发现的那种：
同一条规则前一个入口生效、下一个入口静默绕过，库里的结果却长得像"上游又写回来了"。

三条判据：
  1. W 类窗口外的单点 → 不落库，返回 None（不是"写了又删"）；
  2. W 类窗口内的单点 → 正常落库；
  3. enforce 档下缺契约 → 拒写，抛 RetentionContractMissing。
"""

from datetime import datetime, timedelta, timezone

import pytest

from fd_open_data_mcp.fetch.cache import write_cache
from fd_open_data_mcp.fetch.retention import RetentionContractMissing
from fd_open_data_mcp.models import Concept, DataRetentionContract, SemanticObservation

ENT = ("stock", 1)


def _concept(session, code="aum"):
    c = Concept(code=code, entity_type="stock", frequency="daily",
                unit="USD", verified=True)
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
            .all())


def _days_ago(n: int) -> str:
    return (datetime.now(timezone.utc).date() - timedelta(days=n)).isoformat()


def test_point_write_outside_window_is_dropped(session):
    """W 类窗口外的单点写入：不落库，且明确返回 None。"""
    c = _concept(session)
    _contract(session, "akshare", "aum", "W", 30)

    got = write_cache(session, c.id, ENT[0], ENT[1], _days_ago(200), "1.0",
                      "USD", "akshare", granularity="day")

    assert got is None, "窗口外的点应被丢弃并返回 None，不能假装写成功"
    assert _rows(session, c.id) == [], "窗口外的点一行都不该落库"


def test_point_write_inside_window_lands(session):
    """同一契约下窗口内的点照常落库——守卫不能把正常写入一起挡掉。"""
    c = _concept(session)
    _contract(session, "akshare", "aum", "W", 30)

    obs = write_cache(session, c.id, ENT[0], ENT[1], _days_ago(3), "1.0",
                      "USD", "akshare", granularity="day")

    assert obs is not None
    rows = _rows(session, c.id)
    assert len(rows) == 1 and rows[0].value == "1.0"


def test_point_write_missing_contract_refused_in_enforce(session, monkeypatch):
    """enforce 档：没有契约的源连单点写入也要拒——否则闸门只是 range 路径的装饰。"""
    monkeypatch.setenv("FD_RETENTION_GATE", "enforce")
    c = _concept(session, code="uncontracted_metric")

    with pytest.raises(RetentionContractMissing):
        write_cache(session, c.id, ENT[0], ENT[1], _days_ago(1), "1.0",
                    "USD", "no-such-source", granularity="day")

    assert _rows(session, c.id) == []

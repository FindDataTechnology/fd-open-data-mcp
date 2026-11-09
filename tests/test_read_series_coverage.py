"""read_series 默认路径的覆盖声明（decision 2C，change: define-ingest-revision-and-coverage）。

要害：默认路径以前只报"落不了历法的日期"。窗口缺一半与完整返回同一个形状，
消费方无从发问。现在它必须给 `coverage` 与 `degraded`。

它仍然**不回源**（docstring 的老承诺不变），所以 `degraded` 只会在"连覆盖度都算不出来"
时为真——那种情况下说"不知道"，而不是默认完整。
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from fd_open_data_mcp.models import Concept, SemanticObservation


@pytest.fixture
def seeded(session):
    c = Concept(code="price.close", entity_type="stock", frequency="daily",
                unit="currency", verified=True)
    session.add(c)
    session.commit()
    now = datetime.now(timezone.utc)
    for d in ("2026-01-01", "2026-01-02", "2026-01-03"):
        session.add(SemanticObservation(
            concept_id=c.id, entity_type="stock", entity_id=42, date=d,
            granularity="day", value="1.0", unit="currency", source_used="akshare",
            fetched_at=now,
        ))
    session.commit()
    return c.id


def _read_series(cid: int, start: str, end: str) -> dict:
    """直接调工具函数（MCP 装饰器不影响直接调用）。"""
    from fd_open_data_mcp import server

    return server.read_series(cid, "stock", 42, start, end)


def test_default_path_declares_coverage(seeded):
    """默认路径必须带 coverage，且缺口如实列出。"""
    out = _read_series(seeded, "2026-01-01", "2026-01-10")

    assert "coverage" in out, "默认路径必须声明覆盖，不能只有 points"
    cov = out["coverage"]
    assert cov["requested"] == ["2026-01-01", "2026-01-10"]
    assert cov["covered"] == [["2026-01-01", "2026-01-03"]]
    assert cov["missing"], "缺的区间必须出现"
    assert cov["complete"] is False
    assert out["degraded"]["degraded"] is False, "没回源就谈不上降级"
    assert "incomplete" in out.get("note", "")


def test_default_path_complete_window_says_complete(seeded):
    out = _read_series(seeded, "2026-01-01", "2026-01-03")

    assert out["coverage"]["missing"] == []
    assert out["coverage"]["complete"] is True
    assert "incomplete" not in out.get("note", "")


def test_empty_window_still_declares_the_whole_gap(seeded):
    out = _read_series(seeded, "2026-02-01", "2026-02-05")

    assert out["count"] == 0
    assert out["coverage"]["missing"] == [["2026-02-01", "2026-02-05"]]
    assert out["coverage"]["complete"] is False


def test_points_are_untouched_by_the_declaration(seeded):
    """只加声明，不改数据形状：points 仍是原来的 4 键结构。"""
    out = _read_series(seeded, "2026-01-01", "2026-01-03")

    assert out["count"] == 3
    for p in out["points"]:
        assert set(p) == {"date", "value", "unit", "source_used"}


def test_coverage_failure_says_unknown_not_complete(seeded, monkeypatch):
    """算不出覆盖度时必须说"不知道"，不许默默当作完整。"""
    def _boom(*_a, **_kw):
        raise RuntimeError("calendar exploded")

    monkeypatch.setattr("fd_open_data_mcp.fetch.gapfill.series_dates", _boom)

    out = _read_series(seeded, "2026-01-01", "2026-01-03")

    assert out["coverage"]["complete"] is None, "不知道 ≠ 完整"
    assert out["degraded"]["degraded"] is True
    assert "coverage unknown" in out.get("note", "")
    assert out["count"] == 3, "声明失败不得把数据也弄丢"

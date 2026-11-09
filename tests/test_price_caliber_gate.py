"""价格口径探针（tasks 2.1-2.4）。

这里测三件比"能跑"重要得多的事：

1. **只读**：跑完不产生任何文件；
2. **空比对不是通过**：两源没有同日重叠时必须报违规，不能返回零违规；
3. **今天能红**：按已实测的现状（astock 止于 2026-07-10、指数零观测、两源差 1.00 元）
   构造数据，探针必须失败。这条是"判据在做之前就绿＝没验收"的直接落地。
"""
from __future__ import annotations

import os

import pytest

from fd_open_data_mcp.models import Concept, Entity, SemanticObservation
from scripts.price_caliber_gate import main


@pytest.fixture
def store(session, monkeypatch, tmp_path):
    """照实测现状造库：A 股两源同日差 1.00，指数族零观测。"""
    monkeypatch.setenv("FD_OPEN_DATA_MCP_DATABASE_URL", f"sqlite:///{tmp_path/'gate.db'}")
    monkeypatch.delenv("MARKET_DATA_SOURCE_ID", raising=False)
    monkeypatch.delenv("MARKET_DATA_PROVIDER", raising=False)
    monkeypatch.delenv("ASTOCK_SOURCE", raising=False)
    monkeypatch.delenv("RESEARCH_PRICE_SOURCE", raising=False)
    monkeypatch.delenv("DAAS_PRICE_SOURCE", raising=False)
    monkeypatch.delenv("SETTLE_PRICE_SOURCE", raising=False)
    monkeypatch.delenv("DECISION_SETTLE_PRICE_SOURCE", raising=False)

    c = Concept(code="price.close", entity_type="stock", frequency="daily", unit="CNY")
    e = Entity(entity_type="stock", code="600036", name_zh="招商银行")
    session.add_all([c, e])
    session.flush()
    session.add_all(
        [
            SemanticObservation(
                concept_id=c.id, entity_type="stock", entity_id=e.id,
                date="2026-07-10", granularity="day", value="36.73",
                source_used="astock_daily",
            ),
            SemanticObservation(
                concept_id=c.id, entity_type="stock", entity_id=e.id,
                date="2026-07-10", granularity="day", value="37.73",
                source_used="akshare",
            ),
        ]
    )
    session.commit()
    return session


ARGS = ["--today", "2026-07-20", "--json"]


def _run(*extra, expect: int) -> str:
    import contextlib
    import io

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = main([*ARGS, *extra])
    assert rc == expect, f"exit={rc}, want {expect}; output={buf.getvalue()[:500]}"
    return buf.getvalue()


def test_unspecified_paths_are_violations(store):
    out = _run("--trading", "astock_daily", expect=1)
    assert "PATH_SOURCE_UNSPECIFIED" in out
    assert "research" in out and "settle" in out


def test_unregistered_path_source_is_violation(store):
    out = _run(
        "--trading", "astock_daily", "--research", "nope", "--settle", "astock_daily",
        expect=1,
    )
    assert "PATH_SOURCE_UNREGISTERED" in out


def test_three_way_divergence_is_violation(store):
    out = _run(
        "--trading", "astock_daily", "--research", "akshare", "--settle", "astock_daily",
        expect=1,
    )
    assert "SOURCE_ID_DIVERGENCE" in out


def test_index_absence_is_red(store):
    out = _run(
        "--trading", "astock_daily", "--research", "astock_daily", "--settle", "astock_daily",
        expect=1,
    )
    assert "INDEX_SERIES_ABSENT" in out


def test_caliber_divergence_is_red(store):
    out = _run(
        "--trading", "astock_daily", "--research", "astock_daily", "--settle", "astock_daily",
        expect=1,
    )
    assert "CALIBER_DIVERGENCE" in out
    assert "36.73" in out and "37.73" in out


def test_empty_overlap_is_not_a_pass(session, monkeypatch, tmp_path):
    """首选源在篮内无数据 → 跨源比对为空 → 必须报违规，不能零违规通过。"""
    monkeypatch.setenv("FD_OPEN_DATA_MCP_DATABASE_URL", f"sqlite:///{tmp_path/'empty.db'}")
    c = Concept(code="price.close", entity_type="stock", frequency="daily", unit="CNY")
    e = Entity(entity_type="stock", code="600036", name_zh="招商银行")
    session.add_all([c, e])
    session.flush()
    session.add(
        SemanticObservation(
            concept_id=c.id, entity_type="stock", entity_id=e.id,
            date="2026-07-10", granularity="day", value="37.73", source_used="akshare",
        )
    )
    session.commit()
    out = _run(
        "--trading", "astock_daily", "--research", "astock_daily", "--settle", "astock_daily",
        expect=1,
    )
    assert "NO_CROSS_SOURCE_OVERLAP" in out


def test_green_when_everything_converges(session, monkeypatch, tmp_path):
    """全绿 = 同源同水位 + 指数有观测 + 无可比分歧。"""
    monkeypatch.setenv("FD_OPEN_DATA_MCP_DATABASE_URL", f"sqlite:///{tmp_path/'green.db'}")
    c = Concept(code="price.close", entity_type="stock", frequency="daily", unit="CNY")
    ci = Concept(code="price.close", entity_type="index", frequency="daily", unit="point")
    e = Entity(entity_type="stock", code="600036", name_zh="招商银行")
    idx = Entity(entity_type="index", code="000300", name_zh="沪深300")
    session.add_all([c, ci, e, idx])
    session.flush()
    session.add_all(
        [
            SemanticObservation(
                concept_id=c.id, entity_type="stock", entity_id=e.id,
                date="2026-07-10", granularity="day", value="36.73",
                source_used="astock_daily",
            ),
            # 与首选源同日、口径相同 → 差 0，不违规
            SemanticObservation(
                concept_id=c.id, entity_type="stock", entity_id=e.id,
                date="2026-07-10", granularity="day", value="36.73",
                source_used="akshare",
            ),
            SemanticObservation(
                concept_id=ci.id, entity_type="index", entity_id=idx.id,
                date="2026-07-10", granularity="day", value="3980.1",
                source_used="index-seed",
            ),
        ]
    )
    session.commit()
    out = _run(
        "--trading", "astock_daily", "--research", "astock_daily", "--settle", "astock_daily",
        expect=0,
    )
    assert '"ok": true' in out


def test_gate_writes_nothing(store, tmp_path, monkeypatch):
    """spec: Gate writes nothing。跑完前后目录内容必须一致。"""
    before = {p.name for p in tmp_path.iterdir()}
    _run(
        "--trading", "astock_daily", "--research", "astock_daily", "--settle", "astock_daily",
        expect=1,
    )
    after = {p.name for p in tmp_path.iterdir()}
    assert before == after


def test_env_fallback_picks_up_paths(store, monkeypatch):
    monkeypatch.setenv("MARKET_DATA_PROVIDER", "astock_daily")
    monkeypatch.setenv("RESEARCH_PRICE_SOURCE", "astock_daily")
    monkeypatch.setenv("SETTLE_PRICE_SOURCE", "astock_daily")
    out = _run(expect=1)
    # 三路已从 env 解析到 → 不该再报"未配置"；剩下的红必须来自指数与价差
    assert "PATH_SOURCE_UNSPECIFIED" not in out
    assert "INDEX_SERIES_ABSENT" in out

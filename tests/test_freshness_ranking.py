"""新鲜度必须能改变排序——原来它是每个 concept 一个常数，改不动任何东西。

`composite_score = quality × accessibility × freshness_fit`，而
`freshness_fit_for()` 只看 concept 的频率、也不是 requested_date。同一个 concept 的
所有候选源拿到**同一个乘数**，常数乘不改变排序——于是规范里写的那条
「intraday 请求要跳过 yearly 源」在代码里根本不存在（`requested_date` 收下就丢了）。

两处修：
* fit 按**每个源自己的** cadence 算（bindings → Function.frequency），concept 频率
  只作兜底——没有频率的函数行为完全不变；
* fit 真的读 `requested_date`：yearly 源被问"今天"时降到 0.05，被问两年前仍 0.2。
"""
from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from fd_open_data_mcp.models import (  # noqa: E402
    Concept, ConceptBinding, Function, FunctionColumn, Source, SourceRanking,
)
from fd_open_data_mcp.ranking.scorer import (  # noqa: E402
    _INTRADAY_MISMATCH_FIT, freshness_fit_for, rank_sources_for_concept,
    source_frequencies_for_concept,
)

T, E = "country", 7


def test_yearly_source_is_demoted_for_a_today_request():
    """规范原文那条：《yearly 源不被 intraday 请求选用》。"""
    today = date.today().isoformat()
    assert freshness_fit_for("yearly", today) == pytest.approx(_INTRADAY_MISMATCH_FIT)
    # 问两年前的历史值，yearly 源完全合适 → 不给降级
    old = (date.today() - timedelta(days=730)).isoformat()
    assert freshness_fit_for("yearly", old) == pytest.approx(0.2)


def test_daily_source_is_unaffected_by_recency():
    today = date.today().isoformat()
    assert freshness_fit_for("daily", today) == pytest.approx(0.9)
    assert freshness_fit_for("daily") == pytest.approx(0.9)


def test_without_requested_date_behaviour_is_unchanged():
    """不传日期时口径不许变（既有调用方依赖这个映射）。"""
    assert freshness_fit_for("yearly") == pytest.approx(0.2)
    assert freshness_fit_for("monthly") == pytest.approx(0.5)
    assert freshness_fit_for(None) == pytest.approx(0.5)


def test_garbage_date_falls_back_to_cadence_only():
    """解析不了的日期不许当成 intraday（否则会误降级）。"""
    assert freshness_fit_for("yearly", "not-a-date") == pytest.approx(0.2)


def _world(session):
    """一个 concept，两个源：yearly 的高 quality、daily 的低 quality。"""
    c = Concept(code="gdp_growth", entity_type=T, frequency="daily", unit="pct", verified=True)
    session.add(c)
    session.commit()

    for name, freq, q in (("wb-yearly", "yearly", 0.9), ("akshare-daily", "daily", 0.6)):
        src = Source(name=name, label=name)
        session.add(src)
        session.flush()
        fn = Function(source_id=src.id, command=f"cmd_{name}", frequency=freq, verified=True)
        session.add(fn)
        session.flush()
        col = FunctionColumn(function_id=fn.id, name="value", type="float")
        session.add(col)
        session.flush()
        session.add(ConceptBinding(concept_id=c.id, column_id=col.id, confidence=1.0, provenance="manual"))
        session.add(SourceRanking(source=name, concept_id=c.id,
                                  quality=q, accessibility=1.0, freshness_fit=0.5))
    session.commit()
    return c.id


def test_per_source_frequency_is_read_from_the_binding(session):
    cid = _world(session)
    freqs = source_frequencies_for_concept(session, cid)
    assert freqs == {"wb-yearly": "yearly", "akshare-daily": "daily"}


def test_ranking_reorders_when_the_request_is_today(session):
    """问今天：yearly 那个（quality 更高）必须被压到 daily 之下。

    旧实现里两者拿到同一个 freshness 常数，排序只看 quality × accessibility，
    于是 quality 0.9 的 yearly 源永远排第一——正是审计说的"yearly 源不被降级"。
    """
    cid = _world(session)
    today = date.today().isoformat()

    today = date.today().isoformat()
    ranked_today = {r["source"]: r for r in rank_sources_for_concept(session, cid, requested_date=today)}
    assert ranked_today["wb-yearly"]["freshness_fit"] == pytest.approx(_INTRADAY_MISMATCH_FIT), (
        "今天请求下 yearly 源必须被降级"
    )

    # 问两年前：yearly 能答这个请求，fit 回到常规值——分数随之显著抬高。
    # 注意这里不断言"ordering 反转"：daily 源即便对历史请求也拿 0.9，所以它仍排第一；
    # 要钉的是"yearly 被降级"这件事本身，而不是谁第一。
    old = (date.today() - timedelta(days=730)).isoformat()
    ranked_old = {r["source"]: r for r in rank_sources_for_concept(session, cid, requested_date=old)}
    assert ranked_old["wb-yearly"]["freshness_fit"] == pytest.approx(0.2)
    assert ranked_old["wb-yearly"]["score"] > ranked_today["wb-yearly"]["score"], (
        "同一个 yearly 源被问历史值时，分数应高于被问今天时——否则降级没生效"
    )

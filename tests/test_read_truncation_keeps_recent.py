"""长日期列表被截断时，必须留下**最近**的那一段，而不是最早的那一段。

`read()` 用 `MAX_DATES = 366` 防无界派发。旧实现 `dates[:MAX_DATES]` 保留**头部**
（最早的 366 个），于是请求五年日频（约 1825 个日期）会返回一批**止于四年前**的
行，且响应里看不出 2022–2025 被丢掉——客户端问"这个窗口里最新的收盘价"，拿到
一个 2021 年的值，还以为成功了。

`read_series` 早就按"保留最近 N 条 + 给 note"处理；`read()` 因为返回裸 list 没处
放 note，所以这里至少把方向改对：保留尾部。方向错了的错误数据比报错更危险。
"""
from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from fd_open_data_mcp.fetch.cache import write_cache  # noqa: E402
from fd_open_data_mcp.fetch import dispatch as mod  # noqa: E402
from fd_open_data_mcp.models import Concept  # noqa: E402

T, E = "country", 7


def _concept_with_cache(session, dates: list[str], value: str = "1.0") -> int:
    c = Concept(code="cpi", entity_type=T, frequency="daily", unit="idx", verified=True)
    session.add(c)
    session.commit()
    for d in dates:
        write_cache(session, c.id, T, E, d, value, "idx", "s1")
    return c.id


def _daily_dates(n: int) -> list[str]:
    start = date(2019, 1, 1)
    return [(start + timedelta(days=i)).isoformat() for i in range(n)]


def test_truncation_keeps_the_most_recent_dates(session, monkeypatch):
    """超过上限时，返回的必须是最近的 366 个日期，不能是最早的。"""
    dates = _daily_dates(500)          # 2019-01-01 起 500 天
    cid = _concept_with_cache(session, dates[-400:])  # 只给尾部 400 天建缓存，减少往返

    # 关掉上游派发：本用例只验证"哪些日期被保留"，不验证取数
    monkeypatch.setattr(mod, "rank_sources_for_concept", lambda *a, **k: [])

    rows = mod.read(session, cid, T, E, dates)
    assert rows, "应当有命中缓存的行走出来"

    got = sorted(r["date"] for r in rows)
    cap = 366
    expected_tail = set(dates[-cap:])
    assert set(got) <= expected_tail, (
        "出现了不属于'最近 366 个日期'的行——截断方向错了："
        f"最早一条 {got[0]}，窗口起点 {dates[0]}"
    )
    # 最关键的一条：窗口最后一天必须在结果里（旧实现会把它丢掉）
    assert dates[-1] in set(got), "窗口最新的一天缺失——这正是旧实现把'最新值'答成往年值的原因"


def test_short_list_is_untouched(session, monkeypatch):
    """没超上限时一个日期都不许少。"""
    dates = _daily_dates(10)
    cid = _concept_with_cache(session, dates)
    monkeypatch.setattr(mod, "rank_sources_for_concept", lambda *a, **k: [])

    rows = mod.read(session, cid, T, E, dates)
    assert {r["date"] for r in rows} == set(dates)

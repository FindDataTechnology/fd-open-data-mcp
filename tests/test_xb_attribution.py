"""跨境查询面空结果四类归因（add-crossborder-vertical-line 2.6，spec
crossborder-vertical-data「采集结果四类归因」的查询端契约）。

「未取得」与「确认没有」在报数面上必须可区分：
- never_held    = 确认没有：该 concept+entity 从未有任何源持有过值；
- gap_in_series = 未取得：源在产、其他日期有值，所请求日期是缺口。

采集端归因（配额/上游/防护/待人工）由 spider 层 `_error_category` 携带
（见 fd-industry-data tests/test_xb_spider_attribution.py），本文件只锁查询端。
"""
from __future__ import annotations

import pytest

from fd_open_data_mcp.fetch.cache import write_cache
from fd_open_data_mcp.fetch.dispatch import read
from fd_open_data_mcp.models import Concept

T, E = "hs_code", 300961


def _concept(session, code: str = "trade.export_flow.us") -> int:
    c = Concept(code=code, entity_type=T, frequency="yearly", unit="usd", verified=True)
    session.add(c)
    session.commit()
    return c.id


def test_never_held_confirmed_absence(session):
    """确认没有：从未有值 → error 行带 series_state=never_held，与未取得可区分。"""
    cid = _concept(session, "trade.export_flow.de")
    rows = read(session, cid, T, E, ["2099"], all_sources=True)
    assert len(rows) == 1
    assert rows[0]["error"] == "no source holds a value"
    assert rows[0]["series_state"] == "never_held"


def test_gap_in_series_distinguishes_from_never_held(session):
    """未取得：其他日期有值、所请求日期缺 → series_state=gap_in_series。"""
    cid = _concept(session, "trade.export_flow.uk")
    write_cache(session, cid, T, E, "2025", 83508986.0, "usd", "un-comtrade")
    rows = read(session, cid, T, E, ["2025", "2099"], all_sources=True)
    by_date = {r["date"]: r for r in rows}
    assert by_date["2025"]["value"] == 83508986.0 and "error" not in by_date["2025"]
    assert by_date["2099"]["error"] == "no source holds a value"
    # 关键断言：同一 concept+entity，「有历史的缺口」与「从未有过」不同态
    assert by_date["2099"]["series_state"] == "gap_in_series"


def test_success_rows_carry_no_series_state(session):
    """成功行不携带 series_state（归因只出现在错误行，避免污染正常报数）。"""
    cid = _concept(session, "trade.export_flow.jp")
    write_cache(session, cid, T, E, "2024", 558752482.0, "usd", "un-comtrade")
    rows = read(session, cid, T, E, ["2024"], all_sources=True)
    assert rows[0]["value"] == 558752482.0
    assert "series_state" not in rows[0]

"""Stream board route (task 5.3): renders three metric groups, bilingual.

The board ties together the surfaces built in P2/P3/P1: stream health (3.6),
storage watermark (2.7), execution distribution. Read-only; ships-dark gate
means tests hit it unauthenticated (PERM_ADMIN). Uses the isolated SQLite
`session` fixture so the route's get_database() sees the new columns/tables.
"""
from __future__ import annotations

from fastapi.testclient import TestClient

from fd_open_data_mcp.fetch.cache import write_cache
from fd_open_data_mcp.models import Concept
from fd_open_data_mcp.panel.app import app

client = TestClient(app)


def _seed(session) -> int:
    c = Concept(code="gdp", entity_type="country", measure="gdp",
                unit="CNY", frequency="yearly")
    session.add(c); session.commit()
    write_cache(session, c.id, "country", 1, "2024-12-31", "7", "CNY", "src_a")
    return c.id


def test_stream_board_renders_three_groups(session):
    _seed(session)
    r = client.get("/panel/stream")
    assert r.status_code == 200
    html = r.text
    assert "流面健康度" in html
    assert "存储水位" in html
    assert "执行模型分布" in html


def test_stream_board_shows_watermark_section(session):
    _seed(session)
    r = client.get("/panel/stream")
    html = r.text
    assert "覆盖概念" in html
    # census not sampled → the pending notice
    assert "census 尚未采样" in html


def test_stream_board_english_locale(session):
    _seed(session)
    r = client.get("/panel/stream", headers={"accept-language": "en"})
    assert r.status_code == 200
    assert "Stream health" in r.text
    assert "Storage watermark" in r.text
    assert "Execution model distribution" in r.text


def test_stream_board_no_health_notice_without_redis(session):
    """REDIS_URL unset in tests → the health section shows the notice, not a
    crash (the board degrades to watermark + distribution)."""
    _seed(session)
    r = client.get("/panel/stream")
    assert "未配置 Redis 流面" in r.text

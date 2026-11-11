"""as-of 回放必须区分「曾覆盖但已淘汰」与「从未覆盖」。

`establish-data-retention-principle` R3：这两件事是两种返回体。此前读路径只看现存行，
被淘汰的点根本不在结果里 —— 于是"按契约删了"在返回体里和"从没有过"完全同形，
下游无从判断"这段序列只是被裁了"还是"这个源本来就不覆盖这里"。

三条判据：
  1. 隔离表里的点 → `vintage_unavailable`，且 detail 带上批次与留存窗口；
  2. 既没现存行、也没隔离记录的点 → 仍然不出现（不是凭空造 `absent`）；
  3. 现存行优先：同一天若库里还有行，按正常回放判定，不被淘汰标记顶替。
"""

from datetime import datetime, timezone

from sqlalchemy import text

from fd_open_data_mcp.fetch.cache import write_cache_range
from fd_open_data_mcp.fetch.vintage import (
    VINTAGE_UNAVAILABLE,
    load_evicted_points,
    read_range_as_of,
)
from fd_open_data_mcp.models import Concept

ENT = ("country", 7)


def _now():
    """as_of 取"现在"：测试里的行是刚写进去的，取过去时点会被正确判成 absent。"""
    return datetime.now(timezone.utc).isoformat()


def _concept(session, code="gdp_evicted"):
    c = Concept(code=code, entity_type="country", frequency="yearly", unit="USD")
    session.add(c)
    session.commit()
    return c


def _quarantine_table(session):
    """单测的 sqlite 库里没有这张表——按生产结构造一张最小的。"""
    session.execute(text("""
        CREATE TABLE IF NOT EXISTS semantic_observations_quarantine (
            id INTEGER PRIMARY KEY,
            concept_id INTEGER, entity_type TEXT, entity_id INTEGER,
            date TEXT, granularity TEXT, value TEXT, unit TEXT,
            source_used TEXT, fetched_at TIMESTAMP, obs_month TEXT,
            declared_type TEXT, entity_actual_type TEXT,
            reason TEXT, batch_id TEXT, quarantined_at TIMESTAMP
        )
    """))
    session.commit()


def _evict_one(session, cid, date, batch="retention-W-worldbank-gdp_evicted"):
    session.execute(text("""
        INSERT INTO semantic_observations_quarantine
          (id, concept_id, entity_type, entity_id, date, granularity, value, unit,
           source_used, fetched_at, obs_month, declared_type, entity_actual_type,
           reason, batch_id, quarantined_at)
        VALUES (9001, :cid, :et, :eid, :d, 'year', '1.0', 'USD', 'worldbank',
                :now, NULL, 'country', 'country', 'retention_window_evicted:W',
                :batch, :now)
    """), {"cid": cid, "et": ENT[0], "eid": ENT[1], "d": date,
           "now": datetime.now(timezone.utc), "batch": batch})
    session.commit()


def test_evicted_point_is_vintage_unavailable_not_absent(session):
    _quarantine_table(session)
    c = _concept(session)
    write_cache_range(session, c.id, ENT[0], ENT[1], {"2024": 2.0}, "USD",
                      "worldbank", granularity="year")
    _evict_one(session, c.id, "2020")

    out = read_range_as_of(session, c.id, ENT[0], ENT[1], "2018", "2025", _now())

    evicted_details = [d for d in out["details"] if d.get("evicted")]
    assert [d["date"] for d in evicted_details] == ["2020"]
    assert evicted_details[0]["status"] == VINTAGE_UNAVAILABLE
    assert "retention-W" in (evicted_details[0]["detail"] or "")
    assert "不等同于「从未覆盖」" in (evicted_details[0]["detail"] or "")
    assert out["vintage"]["evicted_by_retention"] == 1
    # 现值仍在，且没有被淘汰标记污染
    assert [v["date"] for v in out["values"]] == ["2024"]


def test_never_covered_point_is_not_invented(session):
    _quarantine_table(session)
    c = _concept(session, code="gdp_no_history")
    write_cache_range(session, c.id, ENT[0], ENT[1], {"2024": 2.0}, "USD",
                      "worldbank", granularity="year")

    out = read_range_as_of(session, c.id, ENT[0], ENT[1], "2018", "2025", _now())

    assert out["vintage"]["evicted_by_retention"] == 0
    assert not any(d.get("evicted") for d in out["details"])


def test_existing_row_wins_over_evicted_marker(session):
    _quarantine_table(session)
    c = _concept(session, code="gdp_both")
    write_cache_range(session, c.id, ENT[0], ENT[1], {"2020": 3.0}, "USD",
                      "worldbank", granularity="year")
    _evict_one(session, c.id, "2020")      # 同一天既有现存行又有隔离记录

    out = read_range_as_of(session, c.id, ENT[0], ENT[1], "2018", "2025", _now())

    assert out["vintage"]["evicted_by_retention"] == 0
    assert [v["date"] for v in out["values"]] == ["2020"]


def test_missing_quarantine_table_is_tolerated(session):
    """表不存在时返回空，不抛——但也不据此编造任何状态。"""
    c = _concept(session, code="gdp_no_table")
    assert load_evicted_points(session, c.id, ENT[0], ENT[1], "2018", "2025") == {}


def test_server_note_declares_evicted_points(session, monkeypatch):
    """端到端：`read_series(as_of_vintage=…)` 的 note 必须点名被淘汰的点数。

    只在 fetch 层加状态还不够——调用方读的是 server 返回体。少了这一句，
    "序列被裁过"与"这个源不覆盖这里"在对外接口上仍然同形。
    """
    from fd_open_data_mcp import server as srv

    _quarantine_table(session)
    c = _concept(session, code="gdp_e2e")
    write_cache_range(session, c.id, ENT[0], ENT[1], {"2024": 2.0}, "USD",
                      "worldbank", granularity="year")
    _evict_one(session, c.id, "2020", batch="retention-W-worldbank-gdp_e2e")

    monkeypatch.setattr(srv, "_session_scope", lambda: _Ctx(session), raising=False)
    out = srv.read_series(c.id, ENT[0], ENT[1], start="2018", end="2025",
                          as_of_vintage=_now())
    assert "曾覆盖" in (out.get("note") or "")
    assert "已按留存策略淘汰" in (out.get("note") or "")


class _Ctx:
    def __init__(self, s):
        self._s = s

    def __enter__(self):
        return self._s

    def __exit__(self, *a):
        return False

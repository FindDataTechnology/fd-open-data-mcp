"""Tests for next_runs/running_runs projections + the coverage aggregation
(add-panel-crawl-observability, tasks 1.1-1.3)."""
from __future__ import annotations

import datetime as dt

from fd_open_data_mcp.models import (
    Cluster, Concept, CrawlPolicy, PolicyRun, SemanticObservation,
)
from fd_open_data_mcp.visibility import snapshot
from fd_open_data_mcp.visibility.coverage import coverage_by_concept


NOW = dt.datetime(2026, 8, 28, 4, 0, tzinfo=dt.timezone.utc)


def _policy(name, *, cron="0 6 * * *", tz="UTC", enabled=True,
            last_run_at=None, created_at=None):
    return CrawlPolicy(
        name=name, enabled=enabled, concept_ids=[1], entity_type="fund",
        cron_expr=cron, timezone=tz, date_policy={"mode": "since_last"},
        frequency="daily", mode="per_date",
        last_run_at=last_run_at, created_at=created_at)


# --- next_runs ----------------------------------------------------------------

def test_next_runs_sorted_with_tz_and_minutes(session):
    """Policies project to their next fire in their own timezone, nearest first."""
    session.add(_policy("late", cron="0 23 * * *",
                        created_at=dt.datetime(2026, 8, 1)))
    # 06:00 Asia/Shanghai == 22:00 UTC the previous day -> nearest
    session.add(_policy("shanghai", cron="0 6 * * *", tz="Asia/Shanghai",
                        created_at=dt.datetime(2026, 8, 1)))
    session.commit()

    rows = snapshot.next_runs(session, now=NOW)
    assert [r["policy"] for r in rows] == ["shanghai", "late"]
    sh = rows[0]
    # 2026-08-28 06:00 +08:00 == 2026-08-27 22:00 UTC — already past NOW, so
    # croniter rolls to the NEXT day fire
    assert sh["next_fire"].startswith("2026-08-28T22:00:00+00:00")
    assert sh["next_fire_local"].startswith("2026-08-29T06:00:00+08:00")
    assert sh["minutes_until"] > 0


def test_next_runs_uses_last_run_at_base(session):
    """The fire is computed from last_run_at, like _cron_due."""
    # last ran 08-28 05:00 UTC; hourly cron -> next fire 06:00 same day
    session.add(_policy("hourly", cron="0 * * * *",
                        last_run_at=dt.datetime(2026, 8, 28, 5)))
    session.commit()
    rows = snapshot.next_runs(session, now=NOW)
    assert rows[0]["next_fire"].startswith("2026-08-28T06:00:00+00:00")
    assert rows[0]["minutes_until"] == 120


def test_next_runs_excludes_disabled_and_survives_bad_cron(session):
    session.add(_policy("off", enabled=False, created_at=dt.datetime(2026, 8, 1)))
    bad = _policy("bad", created_at=dt.datetime(2026, 8, 1))
    bad.cron_expr = "not a cron"
    session.add(bad)
    session.commit()
    assert snapshot.next_runs(session, now=NOW) == []


# --- running_runs -------------------------------------------------------------

def test_running_runs_counters_and_cluster(session):
    pol = _policy("p", created_at=dt.datetime(2026, 8, 1))
    session.add(pol)
    cl = Cluster(name="gz", api_server="https://gz:6443", namespace="scraw",
                 image="img", capacity=4, enabled=True)
    session.add(cl)
    session.commit()
    session.add(PolicyRun(
        policy_id=pol.id, status="running", cluster_id=cl.id,
        job_ref="gz/job-1", started_at=NOW.replace(tzinfo=None) - dt.timedelta(minutes=10),
        plan_cells=100, rows_attempted=40, rows_new=12))
    session.add(PolicyRun(policy_id=pol.id, status="success",
                          started_at=NOW.replace(tzinfo=None) - dt.timedelta(hours=2)))
    session.commit()

    rows = snapshot.running_runs(session, now=NOW)
    assert len(rows) == 1
    r = rows[0]
    assert r["policy"] == "p" and r["cluster"] == "gz"
    assert r["elapsed_minutes"] == 10
    assert r["rows_attempted"] == 40 and r["rows_new"] == 12


def test_build_snapshot_includes_new_sections(session, monkeypatch):
    monkeypatch.setattr(snapshot, "_probe_cluster", lambda c: True)
    session.add(_policy("p", created_at=dt.datetime(2026, 8, 1)))
    session.commit()
    snap = snapshot.build_snapshot(session)
    assert "running_runs" in snap and "next_runs" in snap
    # digest contract intact: today_scheduled still present
    assert "today_scheduled" in snap


# --- coverage -----------------------------------------------------------------

def _seed_concepts_and_observations(session):
    c1 = Concept(code="price.close", entity_type="stock", name_en="Close",
                 name_zh="收盘价", category="price", frequency="daily")
    c2 = Concept(code="gdp.nominal", entity_type="country", name_en="GDP",
                 name_zh="GDP", category="macro", frequency="quarterly")
    session.add_all([c1, c2])
    session.commit()
    obs = [
        # 3 rows for c1 from two sources, freshest 2026-08-27
        SemanticObservation(concept_id=c1.id, entity_type="stock", entity_id=1,
                            date="2026-08-25", value="1", source_used="eastmoney"),
        SemanticObservation(concept_id=c1.id, entity_type="stock", entity_id=1,
                            date="2026-08-27", value="2", source_used="eastmoney"),
        SemanticObservation(concept_id=c1.id, entity_type="stock", entity_id=2,
                            date="2026-08-26", value="3", source_used="sina"),
        # 1 row for c2
        SemanticObservation(concept_id=c2.id, entity_type="country", entity_id=10,
                            date="2026-06-30", value="99", source_used="wbgapi"),
    ]
    session.add_all(obs)
    session.commit()
    return c1, c2


def test_coverage_aggregates_per_concept(session):
    c1, c2 = _seed_concepts_and_observations(session)
    rows = coverage_by_concept(session)
    assert [r["concept_id"] for r in rows] == [c1.id, c2.id]  # rows desc
    top = rows[0]
    assert top["code"] == "price.close" and top["rows"] == 3
    assert top["latest_date"] == "2026-08-27"
    assert top["sources"] == 2
    assert top["last_fetch"]  # fetched_at default fired
    assert rows[1]["rows"] == 1 and rows[1]["sources"] == 1


def test_coverage_filters(session):
    c1, c2 = _seed_concepts_and_observations(session)
    assert all(r["concept_id"] == c1.id
               for r in coverage_by_concept(session, concept_id=c1.id))
    assert all(r["concept_id"] == c2.id
               for r in coverage_by_concept(session, entity_type="country"))
    assert coverage_by_concept(session, entity_type="fund") == []


def test_coverage_read_only(session):
    _seed_concepts_and_observations(session)
    before = session.query(SemanticObservation).count()
    coverage_by_concept(session)
    session.commit()
    assert session.query(SemanticObservation).count() == before


# --- MCP surfaces (tasks 3.1-3.3) ---------------------------------------------

def _list_tool_names():
    import asyncio
    from fd_open_data_mcp.server import mcp
    return {t.name for t in asyncio.run(mcp.list_tools())}


def _unwrap(result):
    """fastmcp ToolResult — structured_content wraps the value as {'result': …}."""
    sc = getattr(result, "structured_content", None)
    if sc is not None:
        return sc.get("result", sc)
    if isinstance(result, tuple):
        return result[1]
    return getattr(result, "data", result)


def test_data_stats_tool_registered_and_matches_coverage(session):
    _seed_concepts_and_observations(session)
    import asyncio
    from fd_open_data_mcp.server import mcp
    assert "data_stats" in _list_tool_names()

    payload = _unwrap(asyncio.run(mcp.call_tool("data_stats", {})))
    direct = coverage_by_concept(session)
    assert [r["rows"] for r in payload["concepts"]] == [r["rows"] for r in direct]
    assert payload["concepts"][0]["latest_date"] == "2026-08-27"
    # stores section present (read-only: no census rows -> empty list)
    assert payload["stores"] == []

    fpayload = _unwrap(asyncio.run(mcp.call_tool(
        "data_stats", {"entity_type": "country"})))
    assert len(fpayload["concepts"]) == 1
    assert fpayload["concepts"][0]["code"] == "gdp.nominal"


def test_crawl_status_tool_includes_next_runs(session, monkeypatch):
    monkeypatch.setattr(snapshot, "_probe_cluster", lambda c: True)
    monkeypatch.setattr(snapshot, "today_scheduled", lambda s, tz=None: [])
    session.add(_policy("p", tz="Asia/Shanghai",
                        created_at=dt.datetime(2026, 8, 1)))
    session.commit()

    import asyncio
    from fd_open_data_mcp.server import mcp
    payload = _unwrap(asyncio.run(mcp.call_tool("crawl_status", {})))
    assert "next_runs" in payload
    entry = payload["next_runs"][0]
    assert entry["policy"] == "p" and entry["timezone"] == "Asia/Shanghai"
    assert entry["next_fire_local"].endswith("+08:00")

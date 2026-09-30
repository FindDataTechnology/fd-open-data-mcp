"""panel-data-coverage-cache: point-grain aggregation equivalence with the
legacy distinct-concat formulation, cache refresh/upsert/read, the panel
page's cache-first serving with bounded live fallback, and the ``data_stats``
cache/live split."""
from __future__ import annotations

import asyncio
import datetime as dt

from fastapi.testclient import TestClient

from fd_open_data_mcp.models import (
    Concept, ConceptCoverage, SemanticObservation)
from fd_open_data_mcp.visibility.coverage import (
    cached_concept_coverage, coverage_by_concept, refresh_concept_coverage)


def _seed_concept(s, cid, **kw):
    c = Concept(id=cid, code=f"C{cid}", verified=True,
                entity_type="country", **kw)
    s.add(c)
    s.commit()
    return c


def _obs(s, cid, entity_type="country", entity_id=1, date="2026-01-10",
         granularity="day", source="src_a", fetched=None):
    s.add(SemanticObservation(
        concept_id=cid, entity_type=entity_type, entity_id=entity_id,
        date=date, granularity=granularity, value="1",
        source_used=source,
        fetched_at=fetched or dt.datetime(2026, 9, 30, 12, 0)))


def _legacy_coverage(s):
    """The pre-rewrite formulation, computed in python from raw rows:
    distinct (entity_type, entity_id, date, granularity) points per concept."""
    per = {}
    for o in s.query(SemanticObservation).all():
        per.setdefault(o.concept_id, []).append(o)
    out = {}
    for cid, obs in per.items():
        pts = {(o.entity_type, o.entity_id, o.date, o.granularity) for o in obs}
        out[cid] = {
            "rows": len(pts),
            "latest_date": max(o.date for o in obs),
            "last_fetch": max(o.fetched_at for o in obs).isoformat(),
            "sources": len({o.source_used for o in obs}),
        }
    return out


def _unwrap(result):
    sc = getattr(result, "structured_content", None)
    if sc is not None:
        return sc.get("result", sc)
    if isinstance(result, tuple):
        return result[1]
    return getattr(result, "data", result)


def _call(name, args):
    from fd_open_data_mcp.server import mcp
    return _unwrap(asyncio.run(mcp.call_tool(name, args)))


class TestAggregationEquivalence:
    def test_matches_legacy_formulation(self, session):
        _seed_concept(session, 1, name_zh="国内生产总值", category="macro")
        _seed_concept(session, 2)
        # concept 1: coexisting sources on one point, a second granularity on
        # another day, a second entity — three covered points, two sources
        _obs(session, 1, source="src_a")
        _obs(session, 1, source="src_b")
        _obs(session, 1, granularity="month", date="2026-01-01")
        _obs(session, 1, entity_id=2, date="2026-02-02", source="src_b",
             fetched=dt.datetime(2026, 9, 30, 13, 0))
        # concept 2: single row
        _obs(session, 2, date="2025-12-31")
        session.commit()

        legacy = _legacy_coverage(session)
        got = {r["concept_id"]: r for r in coverage_by_concept(session)}
        assert set(got) == set(legacy) == {1, 2}
        for cid, exp in legacy.items():
            for k in ("rows", "latest_date", "last_fetch", "sources"):
                assert got[cid][k] == exp[k], (cid, k)
        assert got[1]["rows"] == 3 and got[1]["sources"] == 2
        assert got[2]["rows"] == 1
        rows = coverage_by_concept(session)
        assert rows[0]["concept_id"] == 1      # ordered by point count desc
        assert rows[0]["name_zh"] == "国内生产总值"

    def test_empty_table(self, session):
        assert coverage_by_concept(session) == []

    def test_filters(self, session):
        _seed_concept(session, 1)
        _seed_concept(session, 2)
        _obs(session, 1, entity_type="country")
        _obs(session, 2, entity_type="stock")
        session.commit()
        assert [r["concept_id"]
                for r in coverage_by_concept(session, entity_type="stock")] == [2]
        assert [r["concept_id"]
                for r in coverage_by_concept(session, concept_id=1)] == [1]


class TestRefreshCache:
    def test_refresh_populates_and_reads(self, session):
        _seed_concept(session, 1)
        _obs(session, 1)
        _obs(session, 1, entity_id=2, date="2026-02-02")
        session.commit()

        out = refresh_concept_coverage(session)
        assert out["status"] == "refreshed"
        assert out["concepts"] == 1 and out["rows_total"] == 2

        cached = cached_concept_coverage(session)
        assert len(cached["concepts"]) == 1
        r = cached["concepts"][0]
        assert r["rows"] == 2 and r["sources"] == 1
        assert r["latest_date"] == "2026-02-02"
        assert cached["sampled_at"] is not None
        assert cached["age_hours"] is not None and cached["age_hours"] >= 0

    def test_refresh_empty_observations_clears_stale_rows(self, session):
        _seed_concept(session, 1)
        _obs(session, 1)
        session.commit()
        refresh_concept_coverage(session)
        assert session.query(ConceptCoverage).count() == 1

        session.query(SemanticObservation).delete()
        session.commit()
        out = refresh_concept_coverage(session)
        assert out["concepts"] == 0
        assert session.query(ConceptCoverage).count() == 0
        assert cached_concept_coverage(session)["concepts"] == []

    def test_refresh_updates_changed_values(self, session):
        _seed_concept(session, 1)
        _obs(session, 1, date="2026-01-10")
        session.commit()
        refresh_concept_coverage(session)
        _obs(session, 1, date="2026-03-30")
        session.commit()
        refresh_concept_coverage(session)
        r = session.query(ConceptCoverage).one()
        assert r.rows == 2 and r.latest_date == "2026-03-30"


class TestPanelPage:
    def test_fallback_then_cached(self, session):
        from fd_open_data_mcp.panel.app import app
        client = TestClient(app)

        _seed_concept(session, 1, name_zh="国内生产总值")
        _obs(session, 1)
        _obs(session, 1, entity_id=2, date="2026-02-02")
        session.commit()

        # empty cache -> bounded live fallback, page still renders
        r = client.get("/panel/data")
        assert r.status_code == 200
        assert "live coverage" in r.text
        assert "国内生产总值" in r.text

        refresh_concept_coverage(session)
        r = client.get("/panel/data")
        assert r.status_code == 200
        assert "coverage sampled" in r.text
        assert "国内生产总值" in r.text
        # fresh sample, no staleness badge (the freshness legend always
        # shows the word "stale" for the >30d bucket — assert the badge text)
        assert "陈旧 stale" not in r.text

    def test_stale_badge(self, session):
        from fd_open_data_mcp.panel.app import app
        client = TestClient(app)
        _seed_concept(session, 1)
        _obs(session, 1)
        session.commit()
        refresh_concept_coverage(session)
        old = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=7)
        session.query(ConceptCoverage).update({"computed_at": old})
        session.commit()
        r = client.get("/panel/data")
        assert r.status_code == 200
        assert "陈旧 stale" in r.text

    def test_manual_refresh_endpoint(self, session):
        from fd_open_data_mcp.panel.app import app
        client = TestClient(app)
        _seed_concept(session, 1)
        _obs(session, 1)
        session.commit()
        r = client.post("/panel/data/coverage/refresh",
                        follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"] == "/panel/data"
        assert session.query(ConceptCoverage).count() == 1

    def test_filtered_view_live(self, session):
        from fd_open_data_mcp.panel.app import app
        client = TestClient(app)
        _seed_concept(session, 1)
        _obs(session, 1, entity_type="stock")
        _obs(session, 1, entity_id=2, entity_type="fund")
        session.commit()
        refresh_concept_coverage(session)
        r = client.get("/panel/data?entity_type=stock")
        assert r.status_code == 200
        assert "live coverage" in r.text


class TestDataStatsTool:
    def test_detail_reads_cache_with_sampled_at(self, session):
        _seed_concept(session, 1)
        _obs(session, 1)
        _obs(session, 1, entity_id=2, date="2026-02-02")
        session.commit()
        refresh_concept_coverage(session)

        payload = _call("data_stats", {"detail": True})
        assert "sampled_at" in payload and payload["sampled_at"]
        assert "live" not in payload
        assert len(payload["concepts"]) == 1
        assert payload["concepts"][0]["rows"] == 2

    def test_detail_empty_cache_falls_back_live(self, session):
        _seed_concept(session, 1)
        _obs(session, 1)
        session.commit()
        payload = _call("data_stats", {"detail": True})
        assert payload.get("live") is True
        assert len(payload["concepts"]) == 1

    def test_filtered_stays_live(self, session):
        _seed_concept(session, 1)
        _obs(session, 1, entity_type="stock")
        _obs(session, 1, entity_id=2, entity_type="fund")
        session.commit()
        refresh_concept_coverage(session)
        payload = _call("data_stats", {"entity_type": "stock"})
        assert "sampled_at" not in payload and "live" not in payload
        assert len(payload["concepts"]) == 1
        assert payload["concepts"][0]["rows"] == 1

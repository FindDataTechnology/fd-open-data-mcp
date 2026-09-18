"""Tests for add-multi-source-observations.

Per-source coexistence under the relaxed ``uq_sem_obs`` key
(concept, entity, date, granularity, source), query-time preferred-source
selection, source-pinned reads, ``all_sources`` comparison, migration data
preservation, planner watermark scoping, and coverage point-dedupe.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, text

from fd_open_data_mcp.fetch.cache import read_cache, read_cache_all, read_cache_range, write_cache
from fd_open_data_mcp.models import (
    Concept, SemanticObservation, SourceRanking,
)

T, E = "country", 3
TODAY = datetime.now(timezone.utc).strftime("%Y-%m-%d")


@pytest.fixture
def C(session):
    """A real concept row (semantic_observations has an FK to concepts)."""
    c = Concept(code="gdp", entity_type=T, frequency="yearly", unit="USD", verified=True)
    session.add(c)
    session.commit()
    return c.id


def _seed_rankings(session, C, ranks: dict[str, float]):
    for src, q in ranks.items():
        session.add(SourceRanking(source=src, concept_id=C, quality=q,
                                  accessibility=0.5, freshness_fit=0.5))
    session.commit()


# --- coexistence under the relaxed key ---------------------------------------


def test_two_sources_coexist(session, C):
    write_cache(session, C, T, E, TODAY, "18.7e12", "USD", "worldbank")
    write_cache(session, C, T, E, TODAY, "23.1e12", "USD", "cnstats")
    rows = session.query(SemanticObservation).filter_by(
        concept_id=C, entity_type=T, entity_id=E, date=TODAY).all()
    assert {r.source_used for r in rows} == {"worldbank", "cnstats"}
    assert {r.value for r in rows} == {"18.7e12", "23.1e12"}


def test_same_source_reupsert_is_idempotent(session, C):
    write_cache(session, C, T, E, TODAY, "v1", "USD", "worldbank")
    write_cache(session, C, T, E, TODAY, "v2", "USD", "worldbank")
    rows = session.query(SemanticObservation).filter_by(
        concept_id=C, entity_type=T, entity_id=E, date=TODAY).all()
    assert len(rows) == 1
    assert rows[0].value == "v2"


def test_write_does_not_touch_other_source_row(session, C):
    write_cache(session, C, T, E, TODAY, "wb-value", "USD", "worldbank")
    wb_row_id = session.query(SemanticObservation).filter_by(
        source_used="worldbank").one().id
    write_cache(session, C, T, E, TODAY, "nbs-value", "USD", "cnstats")
    wb = session.get(SemanticObservation, wb_row_id)
    assert wb.value == "wb-value"  # untouched by the cnstats write


# --- preferred-source reads ---------------------------------------------------


def test_read_prefers_highest_ranked_source(session, C):
    write_cache(session, C, T, E, TODAY, "wb", "USD", "worldbank")
    write_cache(session, C, T, E, TODAY, "nbs", "USD", "cnstats")
    _seed_rankings(session, C, {"worldbank": 0.9, "cnstats": 0.7})
    obs = read_cache(session, C, T, E, TODAY)
    assert obs.source_used == "worldbank"


def test_ranking_change_flips_next_read_without_rewrite(session, C):
    write_cache(session, C, T, E, TODAY, "wb", "USD", "worldbank")
    write_cache(session, C, T, E, TODAY, "nbs", "USD", "cnstats")
    _seed_rankings(session, C, {"worldbank": 0.9, "cnstats": 0.7})
    assert read_cache(session, C, T, E, TODAY).source_used == "worldbank"
    # ranking churn: cnstats overtakes
    session.query(SourceRanking).filter_by(source="cnstats", concept_id=C)\
        .update({"quality": 0.95})
    session.commit()
    row_count = session.query(SemanticObservation).filter_by(
        concept_id=C, entity_type=T, entity_id=E, date=TODAY).count()
    assert read_cache(session, C, T, E, TODAY).source_used == "cnstats"
    assert row_count == session.query(SemanticObservation).filter_by(
        concept_id=C, entity_type=T, entity_id=E, date=TODAY).count()


def test_read_pinned_to_source(session, C):
    write_cache(session, C, T, E, TODAY, "wb", "USD", "worldbank")
    write_cache(session, C, T, E, TODAY, "nbs", "USD", "cnstats")
    _seed_rankings(session, C, {"worldbank": 0.9})
    obs = read_cache(session, C, T, E, TODAY, source="cnstats")
    assert obs.source_used == "cnstats"
    assert obs.value == "nbs"
    assert read_cache(session, C, T, E, TODAY, source="missing") is None


def test_read_cache_all_returns_every_source_ranked(session, C):
    write_cache(session, C, T, E, TODAY, "wb", "USD", "worldbank")
    write_cache(session, C, T, E, TODAY, "nbs", "USD", "cnstats")
    _seed_rankings(session, C, {"worldbank": 0.9, "cnstats": 0.7})
    rows = read_cache_all(session, C, T, E, TODAY)
    assert [r.source_used for r in rows] == ["worldbank", "cnstats"]
    assert read_cache_all(session, C, T, E, "1999-01-01") == []


def test_range_read_dedupes_to_preferred_source(session, C):
    for d in ("2024-01-01", "2024-02-01"):
        write_cache(session, C, T, E, d, "wb", "USD", "worldbank")
        write_cache(session, C, T, E, d, "nbs", "USD", "cnstats")
    _seed_rankings(session, C, {"worldbank": 0.9, "cnstats": 0.7})
    rows = read_cache_range(session, C, T, E, "2024-01-01", "2024-12-31")
    assert [r.source_used for r in rows] == ["worldbank", "worldbank"]


# --- dispatch.read surface -----------------------------------------------------


@pytest.fixture
def dispatch_concept(session):
    c = Concept(code="gdp", entity_type=T, frequency="yearly", unit="USD", verified=True)
    session.add(c)
    session.commit()
    return c


def test_read_all_sources_no_dispatch(session, dispatch_concept, monkeypatch):
    write_cache(session, dispatch_concept.id, T, E, TODAY, "wb", "USD", "worldbank")
    write_cache(session, dispatch_concept.id, T, E, TODAY, "nbs", "USD", "cnstats")
    _seed_rankings(session, dispatch_concept.id, {"worldbank": 0.9, "cnstats": 0.7})

    import fd_open_data_mcp.fetch.dispatch as dispatch_mod

    def _boom(*a, **k):  # dispatch must never be reached in all_sources mode
        raise AssertionError("all_sources must not dispatch")

    monkeypatch.setattr(dispatch_mod, "dispatch_one", _boom)
    out = dispatch_mod.read(session, dispatch_concept.id, T, E,
                            [TODAY, "1999-01-01"], all_sources=True)
    got = [r for r in out if r["date"] == TODAY]
    assert [r["source_used"] for r in got] == ["worldbank", "cnstats"]
    assert all(r.get("from_cache") for r in got)
    missing = [r for r in out if r["date"] == "1999-01-01"]
    assert missing and missing[0].get("error")


def test_read_pinned_dispatch_scoped_to_source(session, dispatch_concept, monkeypatch):
    from fd_open_data_mcp.fetch import dispatch as dispatch_mod

    called = []

    def _fake_fetch(source, command, params, **kw):
        called.append(source)
        raise dispatch_mod.FetchError("down")  # every attempt fails

    monkeypatch.setattr(dispatch_mod, "instrumented_fetch", _fake_fetch)
    out = dispatch_mod.read(session, dispatch_concept.id, T, E, [TODAY], source="cnstats")
    assert out[0].get("error")
    assert called == [] or set(called) <= {"cnstats"}  # never another source


# --- migration: old-schema DB upgrades, data preserved -------------------------


_OLD_DDL = """
CREATE TABLE semantic_observations (
    id INTEGER NOT NULL PRIMARY KEY,
    concept_id INTEGER NOT NULL,
    entity_type VARCHAR(32) NOT NULL,
    entity_id INTEGER NOT NULL,
    date VARCHAR(64) NOT NULL,
    granularity VARCHAR(8) NOT NULL DEFAULT 'day',
    value VARCHAR(255),
    unit VARCHAR(64),
    source_used VARCHAR(64) NOT NULL,
    fetched_at DATETIME NOT NULL,
    CONSTRAINT uq_sem_obs UNIQUE (concept_id, entity_type, entity_id, date, granularity)
)
"""


def _old_engine(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path}/old.db")
    with eng.begin() as conn:
        conn.execute(text(_OLD_DDL))
        conn.execute(text(
            "INSERT INTO semantic_observations (concept_id, entity_type, entity_id, date,"
            " granularity, value, unit, source_used, fetched_at) VALUES"
            " (1, 'country', 2, '2024-12-31', 'year', '42', 'USD', 'worldbank', '2025-01-01')"))
    return eng


def test_migrate_swaps_old_key_preserving_rows(tmp_path):
    from fd_open_data_mcp.migrate import _swap_sem_obs_key, _sem_obs_key_is_source_aware

    eng = _old_engine(tmp_path)
    assert not _sem_obs_key_is_source_aware(eng)
    assert _swap_sem_obs_key(eng)  # swap happened
    assert _sem_obs_key_is_source_aware(eng)
    # every original row survived with its source attribution
    with eng.connect() as conn:
        rows = conn.execute(text(
            "SELECT concept_id, entity_type, entity_id, date, granularity, value,"
            " source_used FROM semantic_observations")).fetchall()
    assert len(rows) == 1 and rows[0][6] == "worldbank"
    # the relaxed key admits a second source for the same point
    with eng.begin() as conn:
        conn.execute(text(
            "INSERT INTO semantic_observations (concept_id, entity_type, entity_id, date,"
            " granularity, value, unit, source_used, fetched_at) VALUES"
            " (1, 'country', 2, '2024-12-31', 'year', '43', 'USD', 'cnstats', '2025-01-01')"))
    with eng.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM semantic_observations")).scalar() == 2
    # re-run is a no-op
    assert _swap_sem_obs_key(eng) == []


def test_fresh_create_all_gets_source_aware_key(session):
    from fd_open_data_mcp.migrate import _sem_obs_key_is_source_aware
    assert _sem_obs_key_is_source_aware(session.get_bind())


# --- planner watermark scoping --------------------------------------------------


def test_watermark_scoped_to_sources(session, C):
    from fd_open_data_mcp.crawl.planner import _watermark
    from datetime import datetime, timezone as tz

    now = datetime.now(tz.utc)
    # yearly-cadence rows (the watermark is queried at its own granularity)
    session.add(SemanticObservation(
        concept_id=C, entity_type=T, entity_id=E, date="2024-12-31",
        granularity="year", value="42", unit="USD", source_used="worldbank",
        fetched_at=now))
    session.add(SemanticObservation(
        concept_id=C, entity_type=T, entity_id=E, date="2023-12-31",
        granularity="year", value="41", unit="USD", source_used="cnstats",
        fetched_at=now))
    session.commit()
    # unfiltered: covered when ANY source holds a point
    assert _watermark(session, C, T, "year") == "2024-12-31"
    # source-scoped plan: only what those sources themselves hold
    assert _watermark(session, C, T, "year", sources=["cnstats"]) == "2023-12-31"
    assert _watermark(session, C, T, "year", sources=["brandnew"]) is None


# --- coverage: points dedupe across sources --------------------------------------


def test_coverage_counts_points_not_rows(session):
    from fd_open_data_mcp.visibility.coverage import coverage_by_concept

    c = Concept(code="gdp", entity_type=T, frequency="yearly", unit="USD", verified=True)
    session.add(c)
    session.commit()
    for d in ("2023-12-31", "2024-12-31"):
        write_cache(session, c.id, T, E, d, "wb", "USD", "worldbank")
        write_cache(session, c.id, T, E, d, "nbs", "USD", "cnstats")
    rows = coverage_by_concept(session, concept_id=c.id)
    assert rows[0]["rows"] == 2          # two points, not four rows
    assert rows[0]["sources"] == 2       # both sources visible

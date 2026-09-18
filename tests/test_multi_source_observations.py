"""Tests for add-multi-source-observations.

Per-source coexistence under the relaxed ``uq_sem_obs`` key
(concept, entity, date, granularity, source), query-time preferred-source
selection, source-pinned reads, ``all_sources`` comparison, migration data
preservation, planner watermark scoping, and coverage point-dedupe.
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, text

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


# --- migration: revision 0002 swaps the key, data preserved (PG) ----------------

PG_HOST, PG_PORT, PG_USER = "127.0.0.1", 55432, "fdtest"
PG_BIN = Path("/opt/homebrew/opt/postgresql@14/bin")
PROJECT_ROOT = Path(__file__).resolve().parent.parent
ALEMBIC_DIR = PROJECT_ROOT / "alembic"
KEY_COLS = {"concept_id", "entity_type", "entity_id", "date", "granularity", "source_used"}


def _pg_reachable() -> bool:
    try:
        with socket.create_connection((PG_HOST, PG_PORT), timeout=1.0):
            return True
    except OSError:
        return False


def _uq_sem_obs_cols(engine) -> set:
    with engine.connect() as conn:
        rows = conn.execute(text(
            "SELECT a.attname FROM pg_constraint c "
            "JOIN pg_class t ON t.oid = c.conrelid "
            "CROSS JOIN LATERAL unnest(c.conkey) AS k(attnum) "
            "JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = k.attnum "
            "WHERE c.conrelid = 'semantic_observations'::regclass "
            "AND c.conname = 'uq_sem_obs'")).fetchall()
    return {r[0] for r in rows}


@pytest.mark.skipif(not _pg_reachable(), reason="scratch PostgreSQL 14 at 127.0.0.1:55432 not running")
def test_revision_swaps_old_key_preserving_rows(tmp_path):
    """A database at the baseline (5-column key — the canonical state of
    2026-09-18) upgrades to head: every row survives with its source
    attribution, the key becomes source-aware, a second source for the same
    point is admitted, and re-upgrading is a no-op."""
    from sqlalchemy.orm import sessionmaker

    from fd_open_data_mcp.models import Concept, SemanticObservation

    dbname = f"fdsl_msobs_{os.urandom(4).hex()}"
    subprocess.run(
        [str(PG_BIN / "createdb"), "-h", PG_HOST, "-p", str(PG_PORT), "-U", PG_USER, dbname],
        check=True, capture_output=True)
    url = f"postgresql+psycopg2://{PG_USER}@{PG_HOST}:{PG_PORT}/{dbname}"
    env = {**os.environ, "FD_OPEN_DATA_MCP_DATABASE_URL": url,
           "FD_OPEN_DATA_MCP_ALEMBIC_DIR": str(ALEMBIC_DIR)}
    eng = create_engine(url)
    try:
        # canonical-equivalent state: baseline only -> 5-column key
        subprocess.run([sys.executable, "-m", "alembic", "upgrade", "0001_schema_baseline"],
                       check=True, capture_output=True, env=env, cwd=PROJECT_ROOT)
        assert "source_used" not in _uq_sem_obs_cols(eng)

        s = sessionmaker(bind=eng)()
        c = Concept(code="gdp", entity_type="country", frequency="yearly",
                    unit="USD", verified=True)
        s.add(c)
        s.commit()
        concept_id = c.id
        s.add(SemanticObservation(
            concept_id=concept_id, entity_type="country", entity_id=3, date="2024-12-31",
            granularity="year", value="42", unit="USD", source_used="worldbank",
            fetched_at=datetime.now(timezone.utc)))
        s.commit()
        s.close()

        # upgrade to head applies the swap revision
        subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"],
                       check=True, capture_output=True, env=env, cwd=PROJECT_ROOT)
        assert _uq_sem_obs_cols(eng) == KEY_COLS

        s = sessionmaker(bind=eng)()
        rows = s.query(SemanticObservation).all()
        assert len(rows) == 1 and rows[0].source_used == "worldbank"
        # the relaxed key admits a second source for the same point
        s.add(SemanticObservation(
            concept_id=concept_id, entity_type="country", entity_id=3, date="2024-12-31",
            granularity="year", value="43", unit="USD", source_used="cnstats",
            fetched_at=datetime.now(timezone.utc)))
        s.commit()
        assert s.query(SemanticObservation).count() == 2
        s.close()

        # re-upgrading is a no-op
        subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"],
                       check=True, capture_output=True, env=env, cwd=PROJECT_ROOT)
        assert _uq_sem_obs_cols(eng) == KEY_COLS
    finally:
        eng.dispose()
        subprocess.run(
            [str(PG_BIN / "dropdb"), "--if-exists", "-h", PG_HOST, "-p", str(PG_PORT),
             "-U", PG_USER, dbname], capture_output=True)


def test_fresh_schema_gets_source_aware_key(session):
    """The sqlite test builder (and any create_all from the current models)
    carries the 6-column key by construction."""
    insp = inspect(session.get_bind())
    found = any(i.get("unique") and set(i.get("column_names") or ()) == KEY_COLS
                for i in insp.get_indexes("semantic_observations"))
    found = found or any(set(u.get("column_names") or ()) == KEY_COLS
                         for u in insp.get_unique_constraints("semantic_observations"))
    assert found


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

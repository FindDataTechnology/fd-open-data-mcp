"""Census collector tests (add-shard-aware-coverage, task 2.2).

The dblink seam is injected (design D5): SQLite has no dblink, no pg catalog.
"""
from __future__ import annotations

import pytest

from fd_open_data_mcp.models import Concept, DataCensus, SemanticObservation
from fd_open_data_mcp.visibility import census


def _probe_ok(conn, server):
    return {"approx_rows": 97_878_069, "chunks": 1974,
            "range_end": "2026-08-27 00:00:00+00"}


def test_probe_sql_is_catalog_only():
    """The shard probe must never read the fact table (runbook OOM landmine)."""
    sql = census._REMOTE_PROBE_SQL
    assert "approximate_row_count" in sql
    assert "timescaledb_information.chunks" in sql
    # the only mention of the fact table is as a function argument / literal
    assert "FROM semantic_observations" not in sql.replace(
        "approximate_row_count('semantic_observations')", "")


def test_refresh_populates_local_and_shards(session, monkeypatch):
    c = Concept(code="price.close", entity_type="stock", frequency="daily")
    session.add(c); session.commit(); session.refresh(c)
    session.add(SemanticObservation(concept_id=c.id, entity_type="stock",
                                    entity_id=1, date="2026-08-27",
                                    value="1", source_used="eastmoney"))
    session.commit()
    monkeypatch.setattr(census, "_shard_servers", lambda conn: ["shard_xinru2", "shard_xinru3"])
    monkeypatch.setattr(census, "_dblink_probe", _probe_ok)

    out = census.refresh_census(session)
    rows = {r.store: r for r in session.query(DataCensus).all()}
    assert rows["gz_master"].kind == "local"
    assert rows["gz_master"].exact is True
    assert rows["gz_master"].approx_rows == 1
    assert rows["shard_xinru2"].exact is False
    assert rows["shard_xinru2"].approx_rows == 97_878_069
    assert rows["shard_xinru2"].chunks == 1974
    assert rows["shard_xinru2"].time_range_end.startswith("2026-08-27")
    assert out["stores"]["shard_xinru3"]["approx_rows"] == 97_878_069
    # idempotent upsert: refresh again -> still one row per store
    census.refresh_census(session)
    assert session.query(DataCensus).count() == 3


def test_one_shard_failing_records_error(session, monkeypatch):
    monkeypatch.setattr(census, "_shard_servers", lambda conn: ["shard_a", "shard_b"])

    def flaky(conn, server):
        if server == "shard_a":
            return {"approx_rows": 5, "chunks": 1, "range_end": "x"}
        raise RuntimeError("connection refused")

    monkeypatch.setattr(census, "_dblink_probe", flaky)
    out = census.refresh_census(session)
    rows = {r.store: r for r in session.query(DataCensus).all()}
    assert rows["shard_a"].approx_rows == 5 and rows["shard_a"].error is None
    assert "connection refused" in rows["shard_b"].error
    assert rows["shard_b"].approx_rows is None
    assert out["stores"]["shard_a"]["approx_rows"] == 5


def test_missing_dblink_hint(session, monkeypatch):
    monkeypatch.setattr(census, "_shard_servers", lambda conn: ["shard_a"])

    def no_dblink(conn, server):
        raise RuntimeError("function dblink(text, text) does not exist")

    monkeypatch.setattr(census, "_dblink_probe", no_dblink)
    census.refresh_census(session)
    row = session.query(DataCensus).filter_by(store="shard_a").one()
    assert "CREATE EXTENSION dblink" in row.error
    # local row still written
    assert session.query(DataCensus).filter_by(store="gz_master").one().exact is True


def test_latest_census_reads_without_collecting(session, monkeypatch):
    session.add(DataCensus(store="gz_master", kind="local", exact=True,
                           approx_rows=42))
    session.commit()

    def boom(*a, **k):
        raise AssertionError("collection must not run on read")

    monkeypatch.setattr(census, "refresh_census", boom)
    out = census.latest_census(session)
    assert out[0]["store"] == "gz_master" and out[0]["approx_rows"] == 42


# ── surfaces (task 3.4) ───────────────────────────────────────────────────────

def test_panel_data_shows_stores_and_refresh(session, monkeypatch):
    from fastapi.testclient import TestClient
    from fd_open_data_mcp.panel.app import app as panel_app

    client = TestClient(panel_app)
    session.add(DataCensus(store="gz_master", kind="local", exact=True,
                           approx_rows=4100486, total_size_bytes=1_747_000_000))
    session.add(DataCensus(store="shard_xinru2", kind="shard", exact=False,
                           approx_rows=97878069, chunks=1974,
                           time_range_end="2026-08-27 00:00:00+00"))
    session.commit()

    r = client.get("/panel/data")
    assert r.status_code == 200
    assert "gz_master" in r.text and "shard_xinru2" in r.text
    assert "≈97,878,069" in r.text          # estimate labeled
    assert "4,100,486" in r.text            # exact, no ≈

    # refresh route writes census rows via the injected probe
    monkeypatch.setattr(census, "_shard_servers", lambda conn: [])
    r = client.post("/panel/data/census/refresh", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/panel/data"
    # local row re-sampled (still one row per store, refreshed sampled_at)
    assert session.query(DataCensus).count() == 2


def test_stale_census_marker(session):
    import datetime as dt
    from fastapi.testclient import TestClient
    from fd_open_data_mcp.panel.app import app as panel_app

    client = TestClient(panel_app)
    session.add(DataCensus(store="gz_master", kind="local", exact=True,
                           approx_rows=1,
                           sampled_at=dt.datetime.utcnow() - dt.timedelta(hours=48)))
    session.commit()
    r = client.get("/panel/data")
    assert "stale" in r.text

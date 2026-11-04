"""Analysis API (task 5.2): rollup-backed aggregate/series + explicit failures.

Pins the four failure classes and the range caps — every failure is an explicit
error, never a silent empty/partial result (concept-fetch caliber).
"""
from __future__ import annotations

import pytest

from fd_open_data_mcp.analysis import api
from fd_open_data_mcp.fetch.cache import write_cache
from fd_open_data_mcp.models import Concept


@pytest.fixture
def concept(session):
    c = Concept(code="gdp", entity_type="country", measure="gdp",
                unit="CNY", frequency="yearly")
    session.add(c)
    session.commit()
    return c


def test_aggregate_reads_rollup(session, concept):
    write_cache(session, concept.id, "country", 1, "2024-12-31", "7", "CNY", "s")
    out = api.aggregate(session)
    assert out["ok"] is True
    row = next(r for r in out["concepts"] if r["concept_id"] == concept.id)
    assert row["rows"] == 1 and row["sources"] == 1


def test_aggregate_not_found(session):
    out = api.aggregate(session, concept_id=99999)
    assert out["ok"] is False and out["error"] == "not_found"


def test_series_returns_points(session, concept):
    write_cache(session, concept.id, "country", 1, "2023-12-31", "5", "CNY", "s")
    write_cache(session, concept.id, "country", 1, "2024-12-31", "7", "CNY", "s")
    out = api.series(session, concept_id=concept.id, entity_type="country",
                     entity_id=1, start="2023-01-01", end="2024-12-31")
    assert out["ok"] is True and out["count"] == 2
    assert [p["date"] for p in out["series"]] == ["2023-12-31", "2024-12-31"]


def test_series_data_not_ready(session, concept):
    """No observations landed for the concept → data_not_ready, not empty-ok."""
    out = api.series(session, concept_id=concept.id, entity_type="country",
                     entity_id=1)
    assert out["ok"] is False and out["error"] == "data_not_ready"


def test_series_range_exceeded(session, concept, monkeypatch):
    write_cache(session, concept.id, "country", 1, "2024-12-31", "7", "CNY", "s")
    monkeypatch.setattr(api, "MAX_SERIES_SPAN_DAYS", 30)
    out = api.series(session, concept_id=concept.id, entity_type="country",
                     entity_id=1, start="2020-01-01", end="2024-12-31")
    assert out["ok"] is False and out["error"] == "range_exceeded"


def test_series_not_found(session):
    out = api.series(session, concept_id=99999, entity_type="country", entity_id=1)
    assert out["ok"] is False and out["error"] == "not_found"


def test_aggregate_backend_unavailable(session, concept, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("db down")
    monkeypatch.setattr(api, "get_backend", boom)
    out = api.aggregate(session)
    assert out["ok"] is False and out["error"] == "backend_unavailable"


def test_series_multi_source_collapses_to_one_point(session, concept):
    """Two sources on the same point → one series entry (point caliber)."""
    write_cache(session, concept.id, "country", 1, "2024-12-31", "1", "CNY", "a")
    write_cache(session, concept.id, "country", 1, "2024-12-31", "2", "CNY", "b")
    out = api.series(session, concept_id=concept.id, entity_type="country",
                     entity_id=1)
    assert out["ok"] is True and out["count"] == 1

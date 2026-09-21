"""read_series tool (spec observation-reading).

Cache-only range reads: validation, entity applicability, dedup by preferred
source, truncation reporting, and — the contract that matters — no upstream
dispatch on any path.
"""
from __future__ import annotations

import pytest

from fd_open_data_mcp.fetch.cache import write_cache
from fd_open_data_mcp.models import Concept

T, E = "country", 42


@pytest.fixture
def concept(session):
    c = Concept(code="series_demo", entity_type=T, frequency="yearly", unit="USD", verified=True)
    session.add(c)
    session.commit()
    return c.id


def _call(concept_id, **overrides):
    """Invoke the MCP tool function (not the transport) with defaults."""
    from fd_open_data_mcp.server import read_series

    args = dict(concept_id=concept_id, entity_type=T, entity_id=E,
                start="2018-01-01", end="2022-12-31")
    args.update(overrides)
    return read_series(**args)


def test_returns_stored_points_in_order(session, concept):
    for year, value in (("2019", "1.9e12"), ("2020", "2.0e12"), ("2021", "2.1e12")):
        write_cache(session, concept, T, E, year, value, "USD", "worldbank")

    out = _call(concept)

    assert out["count"] == 3
    assert [p["date"] for p in out["points"]] == ["2019", "2020", "2021"]
    assert all(p["source_used"] == "worldbank" for p in out["points"])
    assert "note" not in out


def test_empty_window_is_a_note_not_an_error(session, concept):
    out = _call(concept)

    assert out["count"] == 0 and out["points"] == []
    assert "coverage gap" in out["note"]


def test_inverted_window_is_rejected(session, concept):
    with pytest.raises(ValueError, match="after end"):
        _call(concept, start="2022-01-01", end="2021-01-01")


def test_missing_bounds_rejected(session, concept):
    with pytest.raises(ValueError, match="required"):
        _call(concept, start="")


def test_entity_type_mismatch_is_rejected(session, concept):
    with pytest.raises(Exception) as ei:
        _call(concept, entity_type="stock")
    assert "applies to entity_type" in str(ei.value)


def test_deprecated_concept_is_rejected(session, concept):
    row = session.get(Concept, concept)
    row.deprecated = True
    session.commit()

    with pytest.raises(Exception):
        _call(concept)


def test_preferred_source_wins_per_point(session, concept):
    from fd_open_data_mcp.models import SourceRanking

    write_cache(session, concept, T, E, "2020", "wb", "USD", "worldbank")
    write_cache(session, concept, T, E, "2020", "nbs", "USD", "cnstats")
    session.add(SourceRanking(source="worldbank", concept_id=concept, quality=0.9,
                              accessibility=0.9, freshness_fit=0.9))
    session.add(SourceRanking(source="cnstats", concept_id=concept, quality=0.1,
                              accessibility=0.1, freshness_fit=0.1))
    session.commit()

    out = _call(concept)
    assert out["count"] == 1 and out["points"][0]["source_used"] == "worldbank"


def test_oversized_window_truncates_with_a_note(session, concept, monkeypatch):
    import fd_open_data_mcp.server as server

    monkeypatch.setattr(server, "MAX_SERIES_ROWS", 3)
    for year in range(2010, 2020):
        write_cache(session, concept, T, E, str(year), str(year), "USD", "worldbank")

    out = _call(concept, start="2009-01-01", end="2021-12-31")

    assert out["count"] == 3
    assert [p["date"] for p in out["points"]] == ["2017", "2018", "2019"]  # most recent kept
    assert "narrow the window" in out["note"]


def test_never_dispatches_upstream(session, concept, monkeypatch):
    """The whole point: a cache-only read must not reach the fetch layer."""
    import fd_open_data_mcp.fetch.dispatch as dispatch_mod

    def _boom(*args, **kwargs):
        raise AssertionError("read_series must never dispatch upstream")

    monkeypatch.setattr(dispatch_mod, "dispatch_one", _boom)
    monkeypatch.setattr(dispatch_mod, "instrumented_fetch", _boom, raising=False)

    write_cache(session, concept, T, E, "2020", "2.0e12", "USD", "worldbank")
    out = _call(concept)
    assert out["count"] == 1

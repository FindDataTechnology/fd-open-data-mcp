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


# ─── fill_gaps: an explicit opt-in, never the default (design D3) ──────────


@pytest.fixture
def price_concept(session):
    c = Concept(code="price.close", entity_type="stock", frequency="daily",
                unit="currency", verified=True)
    session.add(c)
    session.commit()
    return c.id


def test_default_call_still_never_dispatches(session, price_concept) -> None:
    """The guarantee is now 'by default', and it must still hold exactly."""
    write_cache(session, price_concept, "stock", 7, "2026-03-02", "10.0",
                "currency", "akshare")
    out = _call(price_concept, entity_type="stock", entity_id=7,
                start="2026-01-01", end="2026-06-30")
    assert out["count"] == 1
    # The default path is the untouched cache-only read: no fill fields at all,
    # which is what makes "no surprise fetch" verifiable from the response.
    for field in ("fill_attempted", "fill_failed", "gaps", "fingerprint"):
        assert field not in out, f"a default read must not report {field}"


def test_default_call_on_a_partial_window_reports_no_fetch(session, price_concept) -> None:
    """A partial window without opt-in stays a coverage fact, not a fill."""
    write_cache(session, price_concept, "stock", 7, "2026-03-02", "10.0",
                "currency", "akshare")
    out = _call(price_concept, entity_type="stock", entity_id=7,
                start="2026-01-01", end="2026-06-30", fill_gaps=False)
    assert out["count"] == 1
    assert out.get("fill_attempted") in (None, False)


def test_fill_gaps_true_returns_the_fill_fields(session, price_concept) -> None:
    out = _call(price_concept, entity_type="stock", entity_id=7,
                start="2026-01-01", end="2026-01-31", fill_gaps=True)
    # No bindings exist in this fixture, so nothing can be fetched — but the
    # contract is that the fields are present and the failure is labelled
    # rather than raised.
    assert "gaps" in out
    assert "fill_attempted" in out
    assert "fill_failed" in out
    assert "fingerprint" in out
    assert isinstance(out["gaps"], list)


def test_fill_gaps_rejected_for_non_price_concept(session, concept) -> None:
    """Revised-value concepts must not be pinned by an additive fill."""
    out = _call(concept, fill_gaps=True)
    assert out["fill_attempted"] is False
    assert out.get("note")


def test_filling_twice_on_a_complete_window_is_stable(session, price_concept) -> None:
    """Fingerprint is only returned on the fill path; filling a complete window
    twice must yield the same content both times."""
    write_cache(session, price_concept, "stock", 7, "2026-01-05", "10.0",
                "currency", "akshare")
    args = dict(entity_type="stock", entity_id=7,
                start="2026-01-01", end="2026-01-10", fill_gaps=True)
    first = _call(price_concept, **args)
    second = _call(price_concept, **args)
    assert first["fingerprint"] == second["fingerprint"]
    assert first["points"] == second["points"]

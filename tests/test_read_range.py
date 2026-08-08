"""Unit tests for ``read_range`` — bulk series fetch with read-through cache.

Seam under test: ``fetch/dispatch.py::read_range`` / ``_read_range_one``.
The upstream network call is mocked at ``dispatch_mod.instrumented_fetch``
(the same chokepoint the real dispatch routes through), so these tests cover
param building, series extraction, bulk cache write, cache-hit short-circuit,
staleness-triggered refetch, and ranked real_source failover — without the
proxy/circuit layer (covered by test_real_source_failover.py).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest

import fd_open_data_mcp.fetch.dispatch as dispatch_mod
from fd_open_data_mcp.fetch.dispatch import read_range
from fd_open_data_mcp.fetch.runner import FetchError
from fd_open_data_mcp.models import (
    Concept,
    ConceptBinding,
    EntitySourceIdentifier,
    Function,
    FunctionColumn,
    SemanticObservation,
    Source,
)

ENTITY_TYPE = "stock"
ENTITY_ID = 1
START = "2024-07-22"
END = "2024-07-26"

# One trading week of eastmoney-style daily bars (日期 col, 'YYYY-MM-DD' strings).
WEEK_DF = pd.DataFrame({
    "日期": ["2024-07-22", "2024-07-23", "2024-07-24", "2024-07-25", "2024-07-26"],
    "收盘": [1800.0, 1810.5, 1822.0, 1815.0, 1850.0],
})


@pytest.fixture
def catalog(session):
    """Minimal ontology: akshare stock_zh_a_hist -> price.close for one A-share.

    Built directly (not via import_provider) so the tests don't depend on the
    sibling fd-akshare registry being present on this machine.
    """
    src = Source(name="akshare", label="AKShare")
    session.add(src)
    session.flush()
    fn = Function(
        source_id=src.id,
        command="stock_zh_a_hist",
        category="price-history",
        verified=True,
        frequency="daily",
        real_sources=[
            {"name": "eastmoney", "priority": 0},
            {"name": "tencent", "priority": 1},
        ],
    )
    session.add(fn)
    session.flush()
    col = FunctionColumn(function_id=fn.id, name="收盘", type="float")
    session.add(col)
    session.flush()
    concept = Concept(
        code="price.close", entity_type="stock", frequency="daily",
        unit="CNY", verified=True,
    )
    session.add(concept)
    session.flush()
    session.add(ConceptBinding(
        concept_id=concept.id, column_id=col.id,
        confidence=1.0, provenance="manual",
    ))
    session.add(EntitySourceIdentifier(
        entity_type=ENTITY_TYPE, entity_id=ENTITY_ID,
        source="akshare", identifier="600519",
    ))
    session.commit()
    return concept


def _fake_fetch_ok(calls: list, df=WEEK_DF):
    def fake(source, command, params, **kwargs):
        calls.append({
            "source": source, "command": command,
            "params": params, "real_source": kwargs.get("real_source"),
        })
        return df
    return fake


def test_cold_range_fetches_once_and_bulk_writes_cache(session, catalog, monkeypatch):
    """Cold cache: one upstream range call covers the whole window and every
    extracted date lands in semantic_observations."""
    calls: list = []
    monkeypatch.setattr(dispatch_mod, "instrumented_fetch", _fake_fetch_ok(calls))

    out = read_range(session, [catalog.id], ENTITY_TYPE, ENTITY_ID, START, END)

    rows = out[catalog.id]
    assert [r["date"] for r in rows] == [
        "2024-07-22", "2024-07-23", "2024-07-24", "2024-07-25", "2024-07-26",
    ]
    assert [r["value"] for r in rows] == [1800.0, 1810.5, 1822.0, 1815.0, 1850.0]
    assert all(r["unit"] == "CNY" for r in rows)
    assert all(r["source_used"] == "akshare" for r in rows)

    # ONE upstream call for the whole range (not one per date), primary real_source.
    assert len(calls) == 1
    assert calls[0]["command"] == "stock_zh_a_hist"
    assert calls[0]["real_source"] == "eastmoney"
    # akshare adapter range params: compact YYYYMMDD start/end, daily period.
    assert calls[0]["params"]["start_date"] == "20240722"
    assert calls[0]["params"]["end_date"] == "20240726"
    assert calls[0]["params"]["symbol"] == "600519"

    # Bulk cache write: one observation per bar date.
    cached = session.query(SemanticObservation).filter_by(
        concept_id=catalog.id, entity_type=ENTITY_TYPE, entity_id=ENTITY_ID,
    ).all()
    assert {o.date for o in cached} == {r["date"] for r in rows}


def test_fresh_cache_hit_skips_fetch(session, catalog, monkeypatch):
    """Second read of a fresh range is served from cache with no upstream call."""
    calls: list = []
    monkeypatch.setattr(dispatch_mod, "instrumented_fetch", _fake_fetch_ok(calls))

    first = read_range(session, [catalog.id], ENTITY_TYPE, ENTITY_ID, START, END)
    assert len(calls) == 1

    second = read_range(session, [catalog.id], ENTITY_TYPE, ENTITY_ID, START, END)
    assert len(calls) == 1  # no new fetch
    assert second[catalog.id] == first[catalog.id]


def test_stale_cache_row_triggers_refetch(session, catalog, monkeypatch):
    """Any stale row in the range => the whole range is re-fetched and the cache
    refreshed (partial-coverage detection is by staleness, documented caveat)."""
    calls: list = []
    monkeypatch.setattr(dispatch_mod, "instrumented_fetch", _fake_fetch_ok(calls))
    read_range(session, [catalog.id], ENTITY_TYPE, ENTITY_ID, START, END)
    assert len(calls) == 1

    stale_ts = datetime.now(timezone.utc) - timedelta(hours=48)
    row = session.query(SemanticObservation).filter_by(
        concept_id=catalog.id, date="2024-07-24",
    ).one()
    row.fetched_at = stale_ts
    session.commit()

    out = read_range(session, [catalog.id], ENTITY_TYPE, ENTITY_ID, START, END)
    assert len(calls) == 2  # refetched
    assert len(out[catalog.id]) == 5
    refreshed = session.query(SemanticObservation).filter_by(
        concept_id=catalog.id, date="2024-07-24",
    ).one()
    assert refreshed.fetched_at.replace(tzinfo=timezone.utc) > stale_ts


def test_ranked_failover_across_real_sources(session, catalog, monkeypatch):
    """Primary real_source (eastmoney) fails => fail over to tencent, whose
    series is returned and cached with the library source attribution."""
    calls: list = []

    def flaky(source, command, params, **kwargs):
        calls.append(kwargs.get("real_source"))
        if len(calls) == 1:
            raise FetchError("eastmoney blocked")
        return WEEK_DF

    monkeypatch.setattr(dispatch_mod, "instrumented_fetch", flaky)

    out = read_range(session, [catalog.id], ENTITY_TYPE, ENTITY_ID, START, END)

    assert calls == ["eastmoney", "tencent"]  # priority order
    assert len(out[catalog.id]) == 5
    assert out[catalog.id][-1]["value"] == 1850.0


def test_empty_and_degenerate_ranges(session, catalog, monkeypatch):
    """start > end and unknown concepts yield empty lists; an upstream that
    returns nothing in range yields [] and writes nothing."""
    calls: list = []
    monkeypatch.setattr(dispatch_mod, "instrumented_fetch", _fake_fetch_ok(calls))

    # inverted range: no fetch at all
    out = read_range(session, [catalog.id], ENTITY_TYPE, ENTITY_ID, END, START)
    assert out == {catalog.id: []}
    assert calls == []

    # unknown concept id: key present, empty series
    out = read_range(session, [catalog.id, 9999], ENTITY_TYPE, ENTITY_ID, START, END)
    assert set(out) == {catalog.id, 9999}
    assert out[9999] == []

    # upstream covers nothing in the requested range
    empty_df = pd.DataFrame({"日期": ["2020-01-02"], "收盘": [900.0]})
    calls.clear()
    monkeypatch.setattr(dispatch_mod, "instrumented_fetch", _fake_fetch_ok(calls, empty_df))
    out = read_range(session, [9999], ENTITY_TYPE, ENTITY_ID, START, END)
    assert out[9999] == []
    # no observations written for a failed fetch
    assert session.query(SemanticObservation).filter_by(concept_id=9999).count() == 0


def test_entity_type_mismatch_raises(session, catalog):
    with pytest.raises(Exception, match="entity_type"):
        read_range(session, [catalog.id], "fund", ENTITY_ID, START, END)

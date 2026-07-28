"""End-to-end concept-fetch: cache hit, dispatch, failover (task 9.3).

Uses a mock upstream runner so the test is deterministic and network-free.
"""
import pandas as pd
import pytest

import fd_open_data_mcp.fetch.dispatch as dispatch_mod
from fd_open_data_mcp.fetch.dispatch import read
from fd_open_data_mcp.models import (
    Concept, ConceptBinding, EntitySourceIdentifier, Function, FunctionColumn, Source,
)


def _add_function(session, source_name, command, col_name):
    src = Source(name=source_name, label=source_name)
    session.add(src)
    session.flush()
    fn = Function(
        source_id=src.id, command=command, verified=True, scanner_mode="upstream-curated",
        parameters=[{"name": "symbol", "required": True}],
    )
    session.add(fn)
    session.flush()
    col = FunctionColumn(function_id=fn.id, name=col_name, type="float")
    session.add(col)
    session.flush()
    return src, fn, col


def test_read_dispatches_then_caches(session, monkeypatch):
    src, fn, col = _add_function(session, "akshare", "stock_zh_a_hist", "收盘")
    c = Concept(code="price.close", entity_type="stock", unit="currency", frequency="daily")
    session.add(c)
    session.flush()
    session.add(ConceptBinding(concept_id=c.id, column_id=col.id, confidence=0.9, provenance="llm"))
    session.add(EntitySourceIdentifier(entity_type="stock", entity_id=1, source="akshare", identifier="600519"))
    session.commit()

    df = pd.DataFrame({"日期": ["2024-07-26"], "收盘": [1850.0]})
    calls = []

    def fake_run(source, command, params):
        calls.append((source, command, params))
        return df

    monkeypatch.setattr(dispatch_mod, "run_upstream", fake_run)

    res = read(session, c.id, "stock", 1, ["2024-07-26"])
    assert res[0]["value"] == 1850.0
    assert res[0]["source_used"] == "akshare"
    assert res[0]["from_cache"] is False
    assert len(calls) == 1

    # second read -> cache hit, no new upstream call
    res2 = read(session, c.id, "stock", 1, ["2024-07-26"])
    assert res2[0]["from_cache"] is True
    assert float(res2[0]["value"]) == 1850.0
    assert len(calls) == 1


def test_read_failover_on_primary_failure(session, monkeypatch):
    """Primary source fails -> fallback source succeeds (spec concept-fetch)."""
    _asrc, afn, acol = _add_function(session, "akshare", "stock_zh_a_hist", "收盘")
    _ysrc, yfn, ycol = _add_function(session, "yfinance", "ticker_history", "Close")
    c = Concept(code="price.close", entity_type="stock", unit="currency", frequency="daily")
    session.add(c)
    session.flush()
    session.add(ConceptBinding(concept_id=c.id, column_id=acol.id, confidence=0.9, provenance="llm"))
    session.add(ConceptBinding(concept_id=c.id, column_id=ycol.id, confidence=0.9, provenance="llm"))
    session.add(EntitySourceIdentifier(entity_type="stock", entity_id=1, source="akshare", identifier="600519"))
    session.add(EntitySourceIdentifier(entity_type="stock", entity_id=1, source="yfinance", identifier="600519.SS"))
    session.commit()

    df = pd.DataFrame({"日期": ["2024-07-26"], "Close": [1850.0]})

    def fake_run(source, command, params):
        if source == "akshare":
            raise dispatch_mod.FetchError("429")
        return df

    monkeypatch.setattr(dispatch_mod, "run_upstream", fake_run)

    res = read(session, c.id, "stock", 1, ["2024-07-26"])
    assert res[0]["value"] == 1850.0
    assert res[0]["source_used"] == "yfinance"  # failover


def test_read_type_mismatch_rejected(session):
    """price.close (stock) requested for a country -> EntityTypeMismatch."""
    from fd_open_data_mcp.entities.resolver import EntityTypeMismatch

    c = Concept(code="price.close", entity_type="stock", unit="currency", frequency="daily")
    session.add(c)
    session.commit()
    with pytest.raises(EntityTypeMismatch):
        read(session, c.id, "country", 1, ["2024-07-26"])

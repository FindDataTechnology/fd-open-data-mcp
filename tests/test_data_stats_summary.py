"""Tests for the ``data_stats`` aggregate-summary default
(mcp-read-surface-gaps, task 4.1).

The default call must be an aggregate — no per-concept array sized by the
catalog — and must agree with ``coverage_report`` on the shared figures. The
stored-row figure carries its unit in its name on purpose: "observations"
elsewhere in this surface means distinct observation points, so the two counts
legitimately differ and both are asserted here.
"""
from __future__ import annotations

import asyncio

from fd_open_data_mcp.coverage.inventory import coverage_summary
from fd_open_data_mcp.models import (
    Concept, ConceptBinding, Function, FunctionColumn, SemanticObservation,
    Source,
)
from fd_open_data_mcp.visibility.coverage import coverage_by_concept


def _unwrap(result):
    """fastmcp ToolResult — structured_content wraps the value as {'result': …}."""
    sc = getattr(result, "structured_content", None)
    if sc is not None:
        return sc.get("result", sc)
    if isinstance(result, tuple):
        return result[1]
    return getattr(result, "data", result)


def _call(name, args):
    from fd_open_data_mcp.server import mcp
    return _unwrap(asyncio.run(mcp.call_tool(name, args)))


def _routable_concept(session, code, entity_type, source_name="akshare"):
    """Concept + confirmed binding on a verified function -> dispatch-eligible."""
    c = Concept(code=code, entity_type=entity_type, unit="currency",
                frequency="daily")
    session.add(c)
    src = Source(name=source_name, label=source_name)
    session.add(src)
    session.flush()
    fn = Function(source_id=src.id, command=f"f_{code}", verified=True)
    session.add(fn)
    session.flush()
    col = FunctionColumn(function_id=fn.id, name=f"col_{code}")
    session.add(col)
    session.flush()
    session.add(ConceptBinding(concept_id=c.id, column_id=col.id,
                               confidence=0.9, provenance="manual",
                               reviewed=True))
    session.commit()
    return c


def _obs(concept, entity_type, entity_id, date, value="1", source="eastmoney"):
    return SemanticObservation(concept_id=concept.id, entity_type=entity_type,
                               entity_id=entity_id, date=date, value=value,
                               source_used=source)


def test_summary_is_aggregate_and_agrees_with_coverage_report(session):
    """Default call: aggregate shape, shared figures agree with coverage_report."""
    a = _routable_concept(session, "price.close", "stock")
    session.add_all([
        _obs(a, "stock", 1, "2026-08-25"),
        _obs(a, "stock", 1, "2026-08-26"),
        _obs(a, "stock", 2, "2026-08-27", source="sina"),
    ])
    session.commit()

    payload = _call("data_stats", {})

    # the per-concept array is gone from the unfiltered response
    assert "concepts" not in payload
    # units are explicit: rows here, points in the listing
    assert payload["total_stored_rows"] == 3
    assert payload["concepts_with_observations"] == 1
    assert payload["per_entity_type"] == {"stock": {"stored_rows": 3,
                                                    "concepts": 1}}

    cov = coverage_summary(session)
    assert payload["total_concepts"] == cov["total_concepts"]
    assert payload["covered_concepts"] == cov["covered"] == 1
    assert payload["gap_concepts"] == cov["gap"]
    assert payload["stale_concepts"] == cov["stale"]


def test_summary_counts_unroutable_concepts_separately(session):
    """Rows without a binding: counted in the store, absent from covered."""
    bound = _routable_concept(session, "price.close", "stock")
    unbound = Concept(code="unbound.metric", entity_type="stock",
                      unit="currency", frequency="daily")
    session.add(unbound)
    session.commit()
    session.add_all([
        _obs(bound, "stock", 1, "2026-08-25"),
        _obs(unbound, "stock", 9, "2026-08-25", source="nbs"),
    ])
    session.commit()

    payload = _call("data_stats", {})
    assert payload["concepts_with_observations"] == 2   # both hold rows
    assert payload["covered_concepts"] == 1             # only the bound one
    assert payload["total_stored_rows"] == 2


def test_stored_rows_count_sources_separately_from_points(session):
    """Two sources on one point: 2 stored rows, 1 covered point."""
    a = _routable_concept(session, "price.close", "stock")
    session.add_all([
        _obs(a, "stock", 1, "2026-08-25", source="eastmoney"),
        _obs(a, "stock", 1, "2026-08-25", source="sina"),
    ])
    session.commit()

    assert _call("data_stats", {})["total_stored_rows"] == 2
    points = coverage_by_concept(session)[0]["rows"]
    assert points == 1
    assert coverage_by_concept(session)[0]["sources"] == 2


def test_concept_id_still_returns_the_per_concept_row(session):
    """Narrowed call keeps the drill-down listing the panel relies on."""
    a = _routable_concept(session, "price.close", "stock")
    session.add(_obs(a, "stock", 1, "2026-08-25", source="eastmoney"))
    session.commit()

    payload = _call("data_stats", {"concept_id": a.id})
    assert len(payload["concepts"]) == 1
    row = payload["concepts"][0]
    assert row["concept_id"] == a.id
    assert row["rows"] == 1
    assert row["latest_date"] == "2026-08-25"
    assert row["sources"] == 1
    assert row["last_fetch"]


def test_summary_on_empty_database_is_zero_not_error(session):
    """No concepts, no rows: zeros, no exception (fresh-install path)."""
    payload = _call("data_stats", {})
    assert payload["total_concepts"] == 0
    assert payload["covered_concepts"] == 0
    assert payload["concepts_with_observations"] == 0
    assert payload["total_stored_rows"] == 0
    assert payload["per_entity_type"] == {}

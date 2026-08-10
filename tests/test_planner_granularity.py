"""Planner canonical-date + granularity behavior (fix-observation-time-granularity,
spec crawl-planner delta).

- _next_period_start is canonical YYYY-MM-DD only (no legacy YYYY/YYYY-MM parsing).
- the since_last watermark is computed per concept at its own granularity, so a
  monthly concept advances from its monthly observations, never from a daily row.
"""
from __future__ import annotations

from fd_open_data_protocol.schema import (
    ColumnSpec, ConceptHint, DatasourceManifest, FunctionSpec,
)

from fd_open_data_mcp.catalog.register import register_datasource
from fd_open_data_mcp.crawl.plan import DateRange, EntityScope
from fd_open_data_mcp.crawl.planner import _next_period_start, plan_crawl
from fd_open_data_mcp.models import Concept, SemanticObservation


def _manifest(frequency: str = "monthly") -> DatasourceManifest:
    return DatasourceManifest(
        name="test-src", label="Test Src",
        functions=[FunctionSpec(
            command="get_nav", frequency=frequency,
            parameters=[],
            columns=[ColumnSpec(name="nav", type="float", frequency=frequency)],
        )],
        concepts=[ConceptHint(
            column="nav", concept="fund.nav", entity_type="fund",
            unit="CNY", frequency=frequency,
        )],
    )


def _register(session, frequency="monthly") -> int:
    register_datasource(_manifest(frequency), session)
    return session.query(Concept).filter_by(code="fund.nav", entity_type="fund").first().id


def test_next_period_start_canonical():
    assert _next_period_start("2024-12-31", "yearly") == "2025-01-01"
    assert _next_period_start("2024-06-01", "monthly") == "2024-07-01"
    assert _next_period_start("2024-06-01", "daily") == "2024-06-02"
    assert _next_period_start("2024-06-01", None) == "2024-06-02"


def test_watermark_filters_by_granularity(session):
    """A monthly concept's watermark comes from its monthly row, not a later daily row."""
    cid = _register(session, "monthly")
    session.add(SemanticObservation(
        concept_id=cid, entity_type="fund", entity_id=1, date="2024-06-01",
        granularity="month", value="1.0", source_used="test",
    ))
    session.add(SemanticObservation(
        concept_id=cid, entity_type="fund", entity_id=1, date="2024-07-05",
        granularity="day", value="1.1", source_used="test",
    ))
    session.commit()

    plan = plan_crawl(
        session, [cid], EntityScope(entity_type="fund"),
        DateRange(start=None, end="2024-12-31", frequency="monthly"),
        since_last=True,
    )
    # watermark = 2024-06-01 (month), next period start = 2024-07-01 —
    # NOT the daily 2024-07-05 row
    assert plan.date_range.start == "2024-07-01"
    assert plan.wanted_concepts[0].granularity == "month"


def test_daily_concept_watermark_still_daily(session):
    cid = _register(session, "daily")
    session.add(SemanticObservation(
        concept_id=cid, entity_type="fund", entity_id=1, date="2024-06-01",
        granularity="day", value="1.0", source_used="test",
    ))
    session.commit()

    plan = plan_crawl(
        session, [cid], EntityScope(entity_type="fund"),
        DateRange(start=None, end="2024-12-31", frequency="daily"),
        since_last=True,
    )
    assert plan.date_range.start == "2024-06-02"
    assert plan.wanted_concepts[0].granularity == "day"


def test_legacy_bare_year_does_not_corrupt_monthly_watermark(session):
    """A legacy '2024' row (migration keeps it granularity='day') is ignored by a
    monthly concept's watermark — no corrupt start derived from it."""
    cid = _register(session, "monthly")
    session.add(SemanticObservation(
        concept_id=cid, entity_type="fund", entity_id=1, date="2024",
        granularity="day", value="1.0", source_used="test",
    ))
    session.add(SemanticObservation(
        concept_id=cid, entity_type="fund", entity_id=1, date="2025-02-01",
        granularity="month", value="1.2", source_used="test",
    ))
    session.commit()

    plan = plan_crawl(
        session, [cid], EntityScope(entity_type="fund"),
        DateRange(start=None, end="2026-12-31", frequency="monthly"),
        since_last=True,
    )
    assert plan.date_range.start == "2025-03-01"  # advanced from 2025-02-01, not 2024

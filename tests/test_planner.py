"""Tests for the crawl planner (concepts + scope + dates -> CrawlPlan)."""
from fd_open_data_protocol.schema import (
    ColumnSpec, ConceptHint, DatasourceManifest, FunctionSpec,
)

from fd_open_data_mcp.catalog.register import register_datasource
from fd_open_data_mcp.crawl.plan import DateRange, EntityScope
from fd_open_data_mcp.crawl.planner import plan_crawl
from fd_open_data_mcp.models import Concept


def _now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc)


def _manifest() -> DatasourceManifest:
    return DatasourceManifest(
        name="test-src", label="Test Src",
        functions=[FunctionSpec(
            command="get_hist", frequency="daily",
            parameters=[],
            columns=[ColumnSpec(name="close", type="float", frequency="daily")],
        )],
        concepts=[ConceptHint(
            column="close", concept="price.close", entity_type="stock",
            unit="currency", frequency="daily",
        )],
    )


def _register(session) -> int:
    register_datasource(_manifest(), session)
    return session.query(Concept).filter_by(code="price.close", entity_type="stock").first().id


def test_plan_routable_concept(session):
    cid = _register(session)
    plan = plan_crawl(
        session, [cid],
        EntityScope(entity_type="stock"),
        DateRange(start="2024-01-01", end="2024-12-31", frequency="daily"),
    )
    assert len(plan.wanted_concepts) == 1
    pc = plan.wanted_concepts[0]
    assert pc.concept_id == cid
    assert pc.code == "price.close"
    assert pc.entity_type == "stock"
    assert len(pc.ranked_sources) >= 1
    rs = pc.ranked_sources[0]
    assert rs.source == "test-src"
    assert rs.function_command == "get_hist"
    assert rs.column_name == "close"
    assert plan.unroutable == []
    # filter scope (entity_ids None) -> no identifier coverage checked
    assert plan.unmapped == []
    assert plan.persistence["table"] == "semantic_observations"


def test_plan_unbound_concept_is_unroutable(session):
    cid = _register(session)
    # a concept id that does not exist
    missing = cid + 10_000
    plan = plan_crawl(
        session, [missing],
        EntityScope(entity_type="stock"),
        DateRange(start="2024-01-01", end="2024-12-31"),
    )
    assert plan.wanted_concepts == []
    assert len(plan.unroutable) == 1
    assert plan.unroutable[0]["concept_id"] == missing
    assert "not found" in plan.unroutable[0]["reason"]


def test_plan_entity_type_mismatch_is_unroutable(session):
    cid = _register(session)  # price.close is entity_type=stock
    plan = plan_crawl(
        session, [cid],
        EntityScope(entity_type="country"),  # mismatch
        DateRange(start="2024-01-01", end="2024-12-31"),
    )
    assert plan.wanted_concepts == []
    assert len(plan.unroutable) == 1
    assert "mismatch" in plan.unroutable[0]["reason"]


def test_plan_reports_unmapped_entity(session):
    cid = _register(session)
    plan = plan_crawl(
        session, [cid],
        EntityScope(entity_type="stock", entity_ids=[999]),  # no identifier seeded
        DateRange(start="2024-01-01", end="2024-12-31"),
    )
    assert len(plan.wanted_concepts) == 1
    assert len(plan.unmapped) == 1
    assert plan.unmapped[0]["entity_id"] == 999
    assert plan.unmapped[0]["source"] == "test-src"


def test_plan_is_lazy_no_eager_entity_enumeration(session):
    cid = _register(session)
    plan = plan_crawl(
        session, [cid],
        EntityScope(entity_type="stock"),  # filter scope: all stocks
        DateRange(start="2024-01-01", end="2024-12-31"),
    )
    # the plan carries the scope, not an expanded entity list
    assert plan.entity_scope.entity_ids is None
    data = plan.model_dump(mode="json")
    assert "wanted_concepts" in data and len(data["wanted_concepts"]) == 1


# --- since_last (incremental) tests ---

from fd_open_data_mcp.crawl.planner import _next_period_start, _watermark


def test_next_period_start_yearly():
    # canonical YYYY-MM-DD only (fix-observation-time-granularity); legacy bare 'YYYY'
    # is rejected — the granularity-filtered watermark never feeds it a legacy value
    assert _next_period_start("2025-12-31", "yearly") == "2026-01-01"
    try:
        _next_period_start("2025", "yearly")
        raise AssertionError("bare 'YYYY' should be rejected")
    except ValueError:
        pass


def test_next_period_start_monthly():
    assert _next_period_start("2025-06-15", "monthly") == "2025-07-01"
    assert _next_period_start("2025-06-01", "monthly") == "2025-07-01"
    try:
        _next_period_start("2025-06", "monthly")
        raise AssertionError("bare 'YYYY-MM' should be rejected")
    except ValueError:
        pass


def test_next_period_start_daily():
    assert _next_period_start("2025-06-15", "daily") == "2025-06-16"
    assert _next_period_start("2025-06-15", None) == "2025-06-16"  # default daily


def test_watermark_no_observations(session):
    cid = _register(session)
    wm = _watermark(session, cid, "stock")
    assert wm is None


def test_watermark_with_observations(session):
    from sqlalchemy import text
    cid = _register(session)
    # Insert some observations
    session.execute(text(
        "INSERT INTO semantic_observations (concept_id, entity_type, entity_id, date, value, source_used, fetched_at) "
        "VALUES (:c, 'stock', 1, '2024-12-31', '100', 'test-src', :now), "
        "(:c, 'stock', 2, '2025-06-30', '200', 'test-src', :now)"
    ), {"c": cid, "now": _now()})
    session.commit()
    wm = _watermark(session, cid, "stock")
    assert wm == "2025-06-30"


def test_plan_since_last_resumes_from_watermark(session):
    from sqlalchemy import text
    cid = _register(session)
    # Insert observations through 2024-12-31
    session.execute(text(
        "INSERT INTO semantic_observations (concept_id, entity_type, entity_id, date, value, source_used, fetched_at) "
        "VALUES (:c, 'stock', 1, '2024-12-31', '100', 'test-src', :now)"
    ), {"c": cid, "now": _now()})
    session.commit()
    # Plan with since_last=True, frequency=daily → start should be 2025-01-01
    plan = plan_crawl(
        session, [cid],
        EntityScope(entity_type="stock"),
        DateRange(start=None, end="2025-12-31", frequency="daily"),
        since_last=True,
    )
    assert plan.date_range.start == "2025-01-01"
    assert plan.date_range.end == "2025-12-31"
    assert len(plan.wanted_concepts) == 1


def test_plan_since_last_no_observations_no_start_is_unroutable(session):
    cid = _register(session)
    # No observations, no explicit start → unroutable
    plan = plan_crawl(
        session, [cid],
        EntityScope(entity_type="stock"),
        DateRange(start=None, end="2025-12-31", frequency="daily"),
        since_last=True,
    )
    assert plan.wanted_concepts == []
    assert len(plan.unroutable) == 1
    assert "no prior observations" in plan.unroutable[0]["reason"]


def test_plan_since_last_explicit_start_wins(session):
    from sqlalchemy import text
    cid = _register(session)
    # Insert observations through 2024-12-31
    session.execute(text(
        "INSERT INTO semantic_observations (concept_id, entity_type, entity_id, date, value, source_used, fetched_at) "
        "VALUES (:c, 'stock', 1, '2024-12-31', '100', 'test-src', :now)"
    ), {"c": cid, "now": _now()})
    session.commit()
    # Explicit start wins over since_last (spec: "If both, start wins") — the
    # watermark is only consulted when start is None.
    plan = plan_crawl(
        session, [cid],
        EntityScope(entity_type="stock"),
        DateRange(start="2023-01-01", end="2025-12-31", frequency="daily"),
        since_last=True,
    )
    assert plan.date_range.start == "2023-01-01"


# ─── series mode (add-fund-crawl-control-center, design D6) ─────────────────
def _set_bulk(session, command: str, bulk: bool) -> None:
    from fd_open_data_mcp.models import Function
    fn = session.query(Function).filter_by(command=command).first()
    fn.bulk_history = bulk
    session.commit()


def test_plan_series_mode_refuses_non_bulk(session):
    """Series mode refuses a concept bound only to non-bulk_history functions."""
    cid = _register(session)  # get_hist registers with bulk_history=False (default)
    plan = plan_crawl(
        session, [cid],
        EntityScope(entity_type="stock"),
        DateRange(start="2024-01-01", end="2024-12-31", frequency="daily"),
        mode="series",
    )
    assert plan.mode == "series"
    assert plan.wanted_concepts == []
    assert len(plan.unroutable) == 1
    assert plan.unroutable[0]["concept_id"] == cid
    assert plan.unroutable[0]["reason"] == "no bulk_history source for series mode"


def test_plan_series_mode_routes_bulk(session):
    """A bulk_history function stays routable in series mode."""
    cid = _register(session)
    _set_bulk(session, "get_hist", True)
    plan = plan_crawl(
        session, [cid],
        EntityScope(entity_type="stock"),
        DateRange(start="2024-01-01", end="2024-12-31", frequency="daily"),
        mode="series",
    )
    assert plan.mode == "series"
    assert plan.unroutable == []
    assert len(plan.wanted_concepts) == 1
    assert plan.wanted_concepts[0].ranked_sources[0].function_command == "get_hist"


def test_plan_per_date_mode_ignores_bulk_flag(session):
    """per_date (default) routing is unaffected by the bulk_history flag."""
    cid = _register(session)
    plan = plan_crawl(
        session, [cid],
        EntityScope(entity_type="stock"),
        DateRange(start="2024-01-01", end="2024-12-31", frequency="daily"),
    )
    assert plan.mode == "per_date"
    assert len(plan.wanted_concepts) == 1
    assert plan.unroutable == []


def test_plan_bad_mode_rejected(session):
    import pytest
    cid = _register(session)
    with pytest.raises(ValueError, match="mode"):
        plan_crawl(
            session, [cid],
            EntityScope(entity_type="stock"),
            DateRange(start="2024-01-01", end="2024-12-31"),
            mode="bogus",
        )

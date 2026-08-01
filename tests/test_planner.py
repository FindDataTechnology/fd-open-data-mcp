"""Tests for the crawl planner (concepts + scope + dates -> CrawlPlan)."""
from fd_open_data_protocol.schema import (
    ColumnSpec, ConceptHint, DatasourceManifest, FunctionSpec,
)

from fd_open_data_mcp.catalog.register import register_datasource
from fd_open_data_mcp.crawl.plan import DateRange, EntityScope
from fd_open_data_mcp.crawl.planner import plan_crawl
from fd_open_data_mcp.models import Concept


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

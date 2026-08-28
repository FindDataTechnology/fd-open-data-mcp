"""plan_cells + snapshot-first planning tests (fix-silent-zero-yield-crawls
crawl-planner delta).

Proves: plans report their emitted cell count (0 for an empty plan, exact for
an explicit scope); a bulk_snapshot binding collapses the plan to one cell per
date and flags the PlanSource; an explicit scope that is NOT covered by the
snapshot cross-section keeps the fan-out.
"""
from __future__ import annotations

from fd_open_data_protocol.schema import (
    ColumnSpec, ConceptHint, DatasourceManifest, FunctionSpec,
)

from fd_open_data_mcp.catalog.register import register_datasource
from fd_open_data_mcp.crawl.plan import DateRange, EntityScope
from fd_open_data_mcp.crawl.planner import plan_crawl
from fd_open_data_mcp.models import Concept, EntitySourceIdentifier, Function


def _register(session) -> tuple[int, str]:
    register_datasource(DatasourceManifest(
        name="test-src", label="Test Src",
        functions=[FunctionSpec(
            command="get_hist", frequency="daily",
            parameters=[],
            columns=[ColumnSpec(name="close", type="float", frequency="daily")],
        )],
        concepts=[ConceptHint(column="close", concept="price.close",
                              entity_type="stock", unit="currency", frequency="daily")],
    ), session)
    cid = session.query(Concept).filter_by(code="price.close", entity_type="stock").first().id
    # two stocks carry identifiers for the source
    for eid, ident in ((1, "600000"), (2, "000001")):
        session.add(EntitySourceIdentifier(
            entity_type="stock", entity_id=eid, source="test-src", identifier=ident))
    session.commit()
    return cid, "test-src"


def _seed_ids(session, source: str, ids):
    for eid, ident in ids:
        session.add(EntitySourceIdentifier(
            entity_type="stock", entity_id=eid, source=source, identifier=ident))
    session.commit()


def test_plan_reports_cells_for_explicit_scope(session):
    cid, _ = _register(session)
    plan = plan_crawl(
        session, [cid], EntityScope(entity_type="stock", entity_ids=[1, 2]),
        DateRange(start="2024-01-01", end="2024-01-03", frequency="daily"),
    )
    assert plan.plan_cells == 2 * 3  # 2 entities x 3 daily dates


def test_empty_plan_reports_zero_cells(session):
    cid, _ = _register(session)
    plan = plan_crawl(
        session, [cid], EntityScope(entity_type="stock"),
        DateRange(start=None, end="2024-01-03", frequency="daily"),
        since_last=True,
    )
    # no prior observations + no explicit start -> planner refuses all concepts
    assert plan.wanted_concepts == []
    assert plan.plan_cells == 0


def test_bulk_snapshot_collapses_and_flags(session):
    cid, src = _register(session)
    fn = session.query(Function).filter_by(command="get_hist").one()
    fn.bulk_snapshot = True
    session.commit()
    plan = plan_crawl(
        session, [cid], EntityScope(entity_type="stock", entity_ids=[1, 2]),
        DateRange(start="2024-01-01", end="2024-01-03", frequency="daily"),
    )
    ps = plan.wanted_concepts[0].ranked_sources[0]
    assert ps.bulk_snapshot is True
    # one cell per date regardless of entity count
    assert plan.plan_cells == 3


def test_wider_explicit_scope_keeps_fanout(session):
    cid, _ = _register(session)
    fn = session.query(Function).filter_by(command="get_hist").one()
    fn.bulk_snapshot = True
    session.commit()
    # entity 3 has NO identifier for the source -> scope exceeds the
    # snapshot's cross-section -> no collapse
    plan = plan_crawl(
        session, [cid], EntityScope(entity_type="stock", entity_ids=[1, 2, 3]),
        DateRange(start="2024-01-01", end="2024-01-03", frequency="daily"),
    )
    ps = plan.wanted_concepts[0].ranked_sources[0]
    assert ps.bulk_snapshot is False
    assert plan.plan_cells == 3 * 3


def test_unflagged_function_unaffected(session):
    cid, _ = _register(session)
    plan = plan_crawl(
        session, [cid], EntityScope(entity_type="stock", entity_ids=[1, 2]),
        DateRange(start="2024-01-01", end="2024-01-02", frequency="daily"),
    )
    ps = plan.wanted_concepts[0].ranked_sources[0]
    assert ps.bulk_snapshot is False
    assert plan.plan_cells == 2 * 2

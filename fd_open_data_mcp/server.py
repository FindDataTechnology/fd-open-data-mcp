"""fd-open-data-mcp FastMCP server.

Exposes the ontology tools across six capabilities: catalog import, semantic
layer, entity identity, source ranking, concept-fetch, and scheduled refresh.

Entry: ``python -m fd_open_data_mcp.server``  (FastMCP, stdio transport)
       ``fd-open-data-mcp serve``
"""
from __future__ import annotations

from fastmcp import FastMCP

from fd_open_data_mcp.db import get_database

mcp = FastMCP(name="fd-open-data-mcp")


def _session():
    return get_database().get_session()


# ─── Catalog ────────────────────────────────────────────────────────────────
@mcp.tool
def import_catalog(provider: str | None = None) -> dict:
    """Import one (by name) or all fd-* provider registries into the catalog."""
    from fd_open_data_mcp.catalog.importer import import_all, import_provider

    if provider:
        return import_provider(provider)
    return {"providers": import_all()}


# ─── Semantic layer ─────────────────────────────────────────────────────────
@mcp.tool
def consume_concepts() -> dict:
    """Consume ``indicator_defs`` from fd-entities-indicators into the concepts table."""
    from fd_open_data_mcp.semantic.concepts import consume_indicator_defs

    s = _session()
    try:
        return consume_indicator_defs(s)
    finally:
        s.close()


@mcp.tool
def propose_bindings() -> dict:
    """Propose column->concept bindings (propose-and-confirm; below-threshold withheld)."""
    from fd_open_data_mcp.semantic.bindings import propose_bindings as _propose

    s = _session()
    try:
        return _propose(s)
    finally:
        s.close()


@mcp.tool
def list_concepts(entity_type: str | None = None) -> list[dict]:
    """List concepts (optionally filtered by entity_type)."""
    from fd_open_data_mcp.models import Concept

    s = _session()
    try:
        q = s.query(Concept)
        if entity_type:
            q = q.filter_by(entity_type=entity_type)
        return [c.toDict() for c in q.limit(500).all()]
    finally:
        s.close()


@mcp.tool
def list_bindings(concept_code: str | None = None) -> list[dict]:
    """List column->concept bindings (optionally filtered by concept code)."""
    from fd_open_data_mcp.models import Concept, ConceptBinding

    s = _session()
    try:
        q = s.query(ConceptBinding)
        if concept_code:
            c = s.query(Concept).filter_by(code=concept_code).first()
            if c is not None:
                q = q.filter_by(concept_id=c.id)
        return [b.toDict() for b in q.limit(500).all()]
    finally:
        s.close()


@mcp.tool
def review_bindings() -> list[dict]:
    """Return the below-threshold review queue (propose-and-confirm)."""
    from fd_open_data_mcp.semantic.bindings import review_queue

    s = _session()
    try:
        return [b.toDict() for b in review_queue(s)]
    finally:
        s.close()


@mcp.tool
def confirm_binding(binding_id: int) -> dict:
    """Confirm a binding in the review queue (provenance -> manual)."""
    from fd_open_data_mcp.semantic.bindings import confirm_binding as _confirm

    s = _session()
    try:
        b = _confirm(s, binding_id)
        return b.toDict() if b is not None else {"error": "binding not found"}
    finally:
        s.close()


# ─── Entity identity ────────────────────────────────────────────────────────
@mcp.tool
def seed_entity_identifiers() -> dict:
    """Seed akshare/yfinance (stocks) and worldbank (countries) identifier mappings."""
    from fd_open_data_mcp.entities.resolver import (
        seed_country_identifiers, seed_stock_identifiers,
    )

    s = _session()
    try:
        return {"stocks": seed_stock_identifiers(s), "countries": seed_country_identifiers(s)}
    finally:
        s.close()


@mcp.tool
def resolve_entity(entity_type: str, entity_id: int, source: str) -> dict:
    """Resolve the per-source identifier for an entity (None -> source is skipped)."""
    from fd_open_data_mcp.entities.resolver import resolve_identifier

    s = _session()
    try:
        ident = resolve_identifier(s, entity_type, entity_id, source)
        return {"identifier": ident} if ident else {"identifier": None, "note": "no mapping; source will be skipped"}
    finally:
        s.close()


@mcp.tool
def add_entity_identifier(entity_type: str, entity_id: int, source: str, identifier: str) -> dict:
    """Upsert a per-source entity identifier."""
    from fd_open_data_mcp.entities.resolver import add_identifier

    s = _session()
    try:
        return add_identifier(s, entity_type, entity_id, source, identifier).toDict()
    finally:
        s.close()


# ─── Source ranking ─────────────────────────────────────────────────────────
@mcp.tool
def rank_sources(concept_id: int, requested_date: str | None = None) -> list[dict]:
    """Rank candidate sources for a concept (best-first) with composite scores."""
    from fd_open_data_mcp.ranking.scorer import rank_sources_for_concept

    s = _session()
    try:
        return rank_sources_for_concept(s, concept_id, requested_date)
    finally:
        s.close()


# ─── Concept-fetch ──────────────────────────────────────────────────────────
@mcp.tool
def read(concept_id: int, entity_type: str, entity_id: int, dates: list[str]) -> list[dict]:
    """Read a concept for an entity over dates (read-through cache + ranked dispatch)."""
    from fd_open_data_mcp.fetch.dispatch import read as _read

    s = _session()
    try:
        return _read(s, concept_id, entity_type, entity_id, dates)
    finally:
        s.close()


@mcp.tool
def fetch(concept_id: int, entity_type: str, entity_id: int, date: str) -> dict:
    """Force a fetch for one (concept, entity, date), bypassing cache freshness."""
    from fd_open_data_mcp.refresh.runner import refresh_concept

    s = _session()
    try:
        return refresh_concept(s, concept_id, entity_type, entity_id, date)
    finally:
        s.close()


# ─── Crawl planning ─────────────────────────────────────────────────────────
@mcp.tool
def plan_crawl(
    concept_ids: list[int], entity_type: str, start: str, end: str,
    entity_ids: list[int] | None = None, frequency: str | None = None,
) -> dict:
    """Plan a concept crawl -> CrawlPlan artifact (concepts in, methods out).

    Compiles desired concepts + entity scope + date range into a ranked, failover-aware
    CrawlPlan. Does not fetch. Unroutable concepts (no confirmed binding) and unmapped
    entities (no per-source identifier) are reported, not silently dropped.
    """
    from fd_open_data_mcp.crawl.plan import DateRange, EntityScope
    from fd_open_data_mcp.crawl.planner import plan_crawl as _plan

    s = _session()
    try:
        plan = _plan(
            s, concept_ids,
            EntityScope(entity_type=entity_type, entity_ids=entity_ids or None),
            DateRange(start=start, end=end, frequency=frequency),
        )
        return plan.model_dump(mode="json")
    finally:
        s.close()


# ─── Scheduled refresh ──────────────────────────────────────────────────────
@mcp.tool
def generate_refresh_schedules() -> dict:
    """Generate cron schedules for concepts from their indicator_defs.frequency."""
    from fd_open_data_mcp.refresh.scheduler import generate_schedules

    s = _session()
    try:
        return generate_schedules(s)
    finally:
        s.close()


@mcp.tool
def list_schedules() -> list[dict]:
    """List all refresh schedules."""
    from fd_open_data_mcp.refresh.scheduler import list_schedules as _list

    s = _session()
    try:
        return _list(s)
    finally:
        s.close()


@mcp.tool
def run_schedule(schedule_id: int) -> dict:
    """Run one refresh schedule now (ranked-failover dispatch + execution record)."""
    from fd_open_data_mcp.refresh.runner import run_schedule as _run

    s = _session()
    try:
        return _run(s, schedule_id)
    finally:
        s.close()


@mcp.tool
def list_cnreport_rules(
    indicator: str | None = None, document_type: str | None = None,
    module: str | None = None, kind: str | None = None, limit: int = 100,
) -> dict:
    """Browse cn-report's extraction rules (llm_rules + script_rules)."""
    from fd_open_data_mcp.catalog.cnreport_rules import read_cnreport_rules

    rules, errors = read_cnreport_rules(None, indicator, document_type, module, kind, limit)
    return {"count": len(rules), "rules": rules, "errors": errors}


@mcp.tool
def enumerate_wbgapi_indicators(database: str | None = None) -> dict:
    """Enumerate all WDI indicators into columns + concepts + bindings (idempotent)."""
    from fd_open_data_mcp.catalog.wbgapi_enumerate import enumerate_wbgapi_indicators as _enum

    s = _session()
    try:
        return _enum(s, database)
    finally:
        s.close()


@mcp.tool
def register_datasource(path: str) -> dict:
    """Load a manifest (YAML/JSON/Python) via fd-open-data-protocol and register it."""
    from fd_open_data_protocol.loader import load_catalog

    from fd_open_data_mcp.catalog.register import register_datasource as _register

    manifest = load_catalog(path)
    s = _session()
    try:
        return _register(manifest, s)
    finally:
        s.close()


@mcp.tool
def register_discovered() -> dict:
    """Auto-discover + register manifests from entry points + a datasources/ dir."""
    from fd_open_data_mcp.catalog.register import discover_datasources

    s = _session()
    try:
        results = discover_datasources(s)
        return {"registered": results, "count": len(results)}
    finally:
        s.close()


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()

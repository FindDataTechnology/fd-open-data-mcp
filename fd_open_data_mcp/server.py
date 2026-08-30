"""fd-open-data-mcp FastMCP server.

Exposes the ontology tools across six capabilities: catalog import, semantic
layer, entity identity, source ranking, concept-fetch, and scheduled refresh.

Entry: ``python -m fd_open_data_mcp.server``  (FastMCP, stdio transport)
       ``fd-open-data-mcp serve``
"""
from __future__ import annotations

import hmac
import os

from fastmcp import FastMCP

from fd_open_data_mcp.db import get_database

mcp = FastMCP(
    name="fd-open-data-mcp",
    instructions=(
        "Financial & economic data server. Call the FEWEST tools that answer the question; "
        "do not fan out search tools in parallel (they overlap and return duplicate data).\n"
        "• Known entity code (ticker AAPL, country code CN) -> get_entity(type, code). "
        "Do not use search tools when the code is already known.\n"
        "• Fuzzy / natural-language discovery ('concepts about X', 'Asian inflation') -> "
        "ai_search. It is the superset of semantic_search / semantic_search_entities / "
        "semantic_search_unified (concepts + entities + cached values). Call it ONCE; do not "
        "also call the other three in parallel.\n"
        "• Read a concrete value for entity+date -> read(concept_id, entity_type, entity_id, dates).\n"
        "• Rank / inspect candidate sources for a concept -> rank_sources."
    ),
)

# Attach the crawl-policy control-plane tools (add-fund-crawl-control-center).
from fd_open_data_mcp.policy_tools import register_policy_tools

register_policy_tools(mcp)

# Attach the crawl-visibility tools (add-crawl-visibility): on-demand
# `crawl_status` snapshot, shared with the scan/digest watcher entrypoints.
from fd_open_data_mcp.visibility_tools import register_visibility_tools

register_visibility_tools(mcp)

# Attach the coverage-expansion tools (expand-crawl-coverage): read-only
# `coverage_report` gap inventory, shared with the `coverage` CLI and the
# digest's coverage section.
from fd_open_data_mcp.coverage_tools import register_coverage_tools

register_coverage_tools(mcp)


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


@mcp.tool
def register_datasource(manifest_path: str) -> dict:
    """Register a datasource manifest into the ontology database.

    Args:
        manifest_path: Path to YAML/JSON manifest file, or Python module path "pkg.mod:CATALOG"

    Returns:
        Summary of registration result with counts of registered entities/functions/concepts
    """
    from fd_open_data_protocol.loader import load_catalog
    from fd_open_data_mcp.catalog.register import register_datasource as _register

    s = _session()
    try:
        manifest = load_catalog(manifest_path)
        result = _register(manifest, s)
        # Add entity definitions count for clarity
        return {
            "name": manifest.name,
            "functions": result.get("functions", 0),
            "columns": result.get("columns", 0),
            "concepts": result.get("concepts", 0),
            "bindings": result.get("bindings", 0),
            "entities": result.get("entities", 0),
            "entity_definitions": result.get("entity_definitions", 0),
            "relationships": result.get("relationships", 0),
        }
    finally:
        s.close()


# ─── Write operations ───────────────────────────────────────────────────────
@mcp.tool
def update_entity(
    entity_type: str,
    code: str,
    name_en: str | None = None,
    name_zh: str | None = None,
    metadata: dict | None = None,
) -> dict:
    """Update or create an entity in the ontology database.

    This is an admin tool for updating entity metadata after initial registration.

    Args:
        entity_type: Entity type (country, stock, industry, etc.)
        code: Entity canonical identifier (ISO code, ticker, classification code)
        name_en: Optional English name override
        name_zh: Optional Chinese name override
        metadata: Optional metadata dict (merged with existing)

    Returns:
        {status: "created"|"updated", entity_type, code, id}
    """
    from pydantic import BaseModel, Field

    class UpdateEntityParams(BaseModel):
        entity_type: str = Field(..., description="Entity type")
        code: str = Field(..., description="Canonical ID")
        name_en: str | None = None
        name_zh: str | None = None
        metadata: dict | None = None

    params = UpdateEntityParams(entity_type=entity_type, code=code, name_en=name_en, name_zh=name_zh, metadata=metadata)

    from fd_open_data_mcp.catalog.register import upsert_entity
    from fd_open_data_mcp.models import Entity

    s = _session()
    try:
        # Create a minimal entity spec from params
        class EntitySpec:
            def __init__(self, p):
                self.entity_type = p.entity_type
                self.code = p.code
                self.name_en = p.name_en
                self.name_zh = p.name_zh
                self.metadata = p.metadata
                self.relationships = None

        entity_spec = EntitySpec(params)
        entity, status = upsert_entity(s, entity_spec, source_name="admin_update")
        s.commit()
        return {"status": status, "entity_type": entity.entity_type, "code": entity.code, "id": entity.id}
    except Exception as e:
        s.rollback()
        raise
    finally:
        s.close()


@mcp.tool
def update_concept(
    concept_code: str,
    entity_type: str,
    name_en: str | None = None,
    name_zh: str | None = None,
    verified: bool | None = None,
) -> dict:
    """Update concept metadata (name, verification status).

    Args:
        concept_code: Concept code to update
        entity_type: Entity type filter
        name_en: Optional English name update
        name_zh: Optional Chinese name update
        verified: Optional verified flag update

    Returns:
        {affected_count: N, concept_ids: [...]}
    """
    from fd_open_data_mcp.models import Concept

    s = _session()
    try:
        q = s.query(Concept).filter_by(code=concept_code, entity_type=entity_type)
        affected_count = 0
        concept_ids = []
        for concept in q:
            if name_en is not None and concept.name_en != name_en:
                concept.name_en = name_en
            if name_zh is not None and concept.name_zh != name_zh:
                concept.name_zh = name_zh
            if verified is not None and concept.verified != verified:
                concept.verified = verified
            affected_count += 1
            concept_ids.append(concept.id)

        s.commit()
        return {"affected_count": affected_count, "concept_ids": concept_ids}
    except Exception as e:
        s.rollback()
        raise
    finally:
        s.close()


@mcp.tool
def update_binding(
    column_id: int,
    concept_code: str,
    entity_type: str,
    confidence: float | None = None,
    provenance: str | None = None,
    reviewed: bool | None = None,
) -> dict:
    """Update concept binding confidence/provenance/review status.

    Args:
        column_id: FunctionColumn.id to update
        concept_code: Target concept code
        entity_type: Target entity type
        confidence: Optional confidence score update
        provenance: Optional provenance update (llm/manual/admin)
        reviewed: Optional reviewed flag update

    Returns:
        {affected_count: N, binding_ids: [...]}
    """
    from fd_open_data_mcp.models import ConceptBinding, Concept

    s = _session()
    try:
        # Resolve concept to ID
        concept = s.query(Concept).filter_by(code=concept_code, entity_type=entity_type).first()
        if concept is None:
            return {"error": f"Concept not found: {concept_code}/{entity_type}", "affected_count": 0}

        q = s.query(ConceptBinding).filter_by(concept_id=concept.id, column_id=column_id)
        affected_count = 0
        binding_ids = []

        for binding in q:
            updated = False
            if confidence is not None and binding.confidence != confidence:
                binding.confidence = confidence
                updated = True
            if provenance is not None and binding.provenance != provenance:
                binding.provenance = provenance
                updated = True
            if reviewed is not None and binding.reviewed != reviewed:
                binding.reviewed = reviewed
                updated = True

            if updated:
                affected_count += 1
                binding_ids.append(binding.id)

        s.commit()
        return {"affected_count": affected_count, "binding_ids": binding_ids}
    except Exception as e:
        s.rollback()
        raise
    finally:
        s.close()


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


# ─── Entity graph ────────────────────────────────────────────────────────────
@mcp.tool
def list_entities(entity_type: str, limit: int = 100, offset: int = 0) -> list[dict]:
    """List entities of a specific type."""
    from fd_open_data_mcp.entity_graph_tools import list_entities as _list

    return _list(entity_type, limit, offset)


@mcp.tool
def get_entity(entity_type: str, code: str) -> dict | None:
    """Look up one entity by exact type+code (ticker AAPL, country CN).
    Use when the code is known; do not use search tools for this."""
    from fd_open_data_mcp.entity_graph_tools import get_entity as _get

    return _get(entity_type, code)


@mcp.tool
def add_entity(
    entity_type: str,
    code: str,
    name_en: str | None = None,
    name_zh: str | None = None,
    metadata: dict | None = None,
) -> dict:
    """Add a new entity to the registry."""
    from fd_open_data_mcp.entity_graph_tools import add_entity as _add

    return _add(entity_type, code, name_en, name_zh, metadata)


@mcp.tool
def list_relationships(entity_type: str, code: str, direction: str = "outgoing") -> list[dict]:
    """List relationships for an entity."""
    from fd_open_data_mcp.entity_graph_tools import list_relationships as _list

    return _list(entity_type, code, direction)


@mcp.tool
def add_relationship(
    source_entity_type: str,
    source_code: str,
    relation_type: str,
    target_entity_type: str,
    target_code: str,
    valid_from: str | None = None,
    valid_to: str | None = None,
    metadata: dict | None = None,
) -> dict:
    """Add a new relationship between two entities."""
    from fd_open_data_mcp.entity_graph_tools import add_relationship as _add

    return _add(source_entity_type, source_code, relation_type, target_entity_type, target_code, valid_from, valid_to, metadata)


# ─── Graph search (NetworkX) ─────────────────────────────────────────────────
@mcp.tool
def graph_search(
    algorithm: str,
    start_entity_code: str,
    end_entity_code: str | None = None,
    max_depth: int = 3,
    entity_type_filter: str | None = None,
) -> dict:
    """Perform graph-based entity relationship queries using NetworkX.

    Args:
        algorithm: Graph algorithm to use. Options:
            - "bfs": Breadth-first search traversal
            - "dfs": Depth-first search traversal
            - "neighbors": Get direct neighbors
            - "shortest_path": Find shortest path between two entities
            - "subgraph": Extract subgraph by entity type
            - "ego_graph": Extract ego graph centered on entity
            - "statistics": Get graph statistics
        start_entity_code: Starting entity code (e.g., "AAPL", "CN")
        end_entity_code: Ending entity code (required for shortest_path)
        max_depth: Maximum traversal depth (default: 3)
        entity_type_filter: Filter results by entity type (e.g., "country", "stock")

    Returns:
        Dictionary with query results depending on algorithm:
        - bfs/dfs: List of entities with depth info
        - neighbors: List of neighbor entities with relationship info
        - shortest_path: List of entities in the path
        - subgraph/ego_graph: Dict with nodes and edges
        - statistics: Graph statistics (node count, edge count, etc.)

    Examples:
        # BFS traversal from Apple
        graph_search("bfs", "AAPL", max_depth=2)

        # Shortest path from Apple to China
        graph_search("shortest_path", "AAPL", "CN")

        # Get all neighbors of Apple
        graph_search("neighbors", "AAPL")

        # Get graph statistics
        graph_search("statistics", "")
    """
    from fd_open_data_mcp.graph.manager import EntityGraphManager
    from fd_open_data_mcp.db import get_database

    # Get database URL
    db = get_database()
    database_url = db.database_url

    # Initialize graph manager
    graph_manager = EntityGraphManager(database_url)

    try:
        # Validate algorithm
        valid_algorithms = ["bfs", "dfs", "neighbors", "shortest_path", "subgraph", "ego_graph", "statistics"]
        if algorithm not in valid_algorithms:
            return {
                "error": f"Invalid algorithm: {algorithm}. Valid options: {valid_algorithms}"
            }

        # Handle statistics (doesn't need start_entity_code)
        if algorithm == "statistics":
            return graph_manager.get_statistics()

        # Validate start_entity_code
        if not start_entity_code:
            return {"error": "start_entity_code is required for this algorithm"}

        # Find start node
        start_node = graph_manager.find_node_by_code(start_entity_code, entity_type_filter)
        if start_node is None:
            return {"error": f"Entity not found: {start_entity_code}"}

        # Execute algorithm
        if algorithm == "bfs":
            results = graph_manager.bfs_traversal(start_node, max_depth, entity_type_filter)
            return {
                "algorithm": "bfs",
                "start_entity": start_entity_code,
                "max_depth": max_depth,
                "results": results,
                "count": len(results)
            }

        elif algorithm == "dfs":
            results = graph_manager.dfs_traversal(start_node, max_depth, entity_type_filter)
            return {
                "algorithm": "dfs",
                "start_entity": start_entity_code,
                "max_depth": max_depth,
                "results": results,
                "count": len(results)
            }

        elif algorithm == "neighbors":
            results = graph_manager.get_neighbors(start_node, entity_type_filter)
            return {
                "algorithm": "neighbors",
                "entity": start_entity_code,
                "results": results,
                "count": len(results)
            }

        elif algorithm == "shortest_path":
            if not end_entity_code:
                return {"error": "end_entity_code is required for shortest_path algorithm"}

            end_node = graph_manager.find_node_by_code(end_entity_code, entity_type_filter)
            if end_node is None:
                return {"error": f"Entity not found: {end_entity_code}"}

            results = graph_manager.shortest_path(start_node, end_node)
            if not results:
                return {
                    "algorithm": "shortest_path",
                    "start_entity": start_entity_code,
                    "end_entity": end_entity_code,
                    "results": [],
                    "message": "No path found between entities"
                }

            return {
                "algorithm": "shortest_path",
                "start_entity": start_entity_code,
                "end_entity": end_entity_code,
                "results": results,
                "path_length": len(results) - 1
            }

        elif algorithm == "subgraph":
            if not entity_type_filter:
                return {"error": "entity_type_filter is required for subgraph algorithm"}

            results = graph_manager.get_subgraph_by_type(entity_type_filter, max_nodes=100)
            return {
                "algorithm": "subgraph",
                "entity_type": entity_type_filter,
                "nodes": results["nodes"],
                "edges": results["edges"],
                "node_count": len(results["nodes"]),
                "edge_count": len(results["edges"])
            }

        elif algorithm == "ego_graph":
            results = graph_manager.get_ego_graph(start_node, radius=max_depth)
            return {
                "algorithm": "ego_graph",
                "center_entity": start_entity_code,
                "radius": max_depth,
                "nodes": results["nodes"],
                "edges": results["edges"],
                "node_count": len(results["nodes"]),
                "edge_count": len(results["edges"])
            }

    except Exception as e:
        return {"error": f"Graph search failed: {str(e)}"}


# ─── Semantic search ────────────────────────────────────────────────────────
@mcp.tool
def semantic_search(
    query: str,
    entity_type: str | None = None,
    frequency: str | None = None,
    limit: int = 20,
) -> list[dict]:
    """Search concepts semantically using vector embeddings."""
    from fd_open_data_mcp.semantic_search import semantic_search as _search

    return _search(query, entity_type, frequency, limit)


@mcp.tool
def semantic_search_entities(
    query: str,
    entity_type: str | None = None,
    limit: int = 20,
) -> list[dict]:
    """Search entities semantically using vector embeddings.

    Args:
        query: Natural language query (e.g., "Asian countries", "technology companies")
        entity_type: Optional filter by entity type (country, stock, industry, etc.)
        limit: Maximum number of results to return

    Returns:
        List of entities with similarity scores, sorted by relevance
    """
    from fd_open_data_mcp.semantic.entity_search import EntitySemanticSearch
    from fd_open_data_mcp.db import get_database

    db = get_database()
    search = EntitySemanticSearch(db.database_url)

    return search.search(query, entity_type, limit)


@mcp.tool
def semantic_search_unified(
    query: str,
    entity_type: str | None = None,
    limit: int = 20,
) -> list[dict]:
    """Unified semantic search across both entities and concepts.

    Args:
        query: Natural language query
        entity_type: Optional filter by entity type (applies to entities only)
        limit: Maximum number of results to return

    Returns:
        List of results (entities and concepts) with similarity scores
    """
    from fd_open_data_mcp.semantic.entity_search import EntitySemanticSearch
    from fd_open_data_mcp.db import get_database

    db = get_database()
    search = EntitySemanticSearch(db.database_url)

    return search.search_unified(query, entity_type, limit)


@mcp.tool
def re_embed_concept(concept_id: int) -> dict:
    """Re-embed a single concept."""
    from fd_open_data_mcp.semantic_search import re_embed_concept as _re_embed

    return _re_embed(concept_id)


# ─── AI search (orchestrates all layers) ────────────────────────────────────
@mcp.tool
def ai_search(
    query: str,
    entity_type: str | None = None,
    limit: int = 20,
    include_values: bool = False,
    value_date: str | None = None,
) -> dict:
    """Default search entry point — call this for fuzzy/natural-language discovery.
    Superset of semantic_search/semantic_search_entities/semantic_search_unified
    (concepts + entities + cached values); do not call those in parallel with this.

    Args:
        query: Natural language query (e.g., "Asian inflation indicators")
        entity_type: Filter by entity type (country, stock, industry, etc.)
        limit: Maximum number of concepts to return
        include_values: If True, fetch actual values from semantic_observations
        value_date: Date for values (e.g., "2024-01-01") - only if include_values=True

    Returns:
        Dictionary with concepts, entities, and optionally values
    """
    from fd_open_data_mcp.ai_search import ai_search as _ai_search

    return _ai_search(query, entity_type, limit, include_values, value_date)


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
    concept_ids: list[int], entity_type: str,
    start: str | None = None, end: str | None = None,
    entity_ids: list[int] | None = None, frequency: str | None = None,
    since_last: bool = False,
    mode: str = "per_date",
) -> dict:
    """Plan a concept crawl -> CrawlPlan artifact (concepts in, methods out).

    Compiles desired concepts + entity scope + date range into a ranked, failover-aware
    CrawlPlan. Does not fetch. Unroutable concepts (no confirmed binding) and unmapped
    entities (no per-source identifier) are reported, not silently dropped.

    If ``since_last`` is True, the plan's ``date_range.start`` is derived from the minimum
    per-concept ``max(date)`` already in ``semantic_observations`` (incremental crawl).
    Either ``start`` or ``since_last`` must be provided. If both, ``start`` wins.
    ``end`` defaults to today if not provided.

    ``mode`` is ``per_date`` (one request per concept x entity x date) or ``series``
    (one request per concept x entity against a bulk_history endpoint; the pipeline
    explodes the returned frame). Series mode refuses concepts bound only to non-bulk
    functions.
    """
    import datetime as dt
    from fd_open_data_mcp.crawl.plan import DateRange, EntityScope
    from fd_open_data_mcp.crawl.planner import plan_crawl as _plan

    # Validation
    if not start and not since_last:
        raise ValueError("Either 'start' or 'since_last' must be provided")
    if not end:
        end = dt.date.today().isoformat()
    if start and since_last:
        since_last = False

    s = _session()
    try:
        plan = _plan(
            s, concept_ids,
            EntityScope(entity_type=entity_type, entity_ids=entity_ids or None),
            DateRange(start=start, end=end, frequency=frequency),
            since_last=since_last, mode=mode,
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


class BearerAuthMiddleware:
    """ASGI middleware gating http requests behind a bearer token.

    Active only when ``MCP_BEARER_TOKEN`` is set (read per-request): every
    request must carry ``Authorization: Bearer <token>`` (constant-time
    compare) or it is rejected with 401 before any MCP handling. stdio
    never passes through here, and an unset/empty token disables the gate.
    """

    def __init__(self, asgi_app):
        self.asgi_app = asgi_app

    def __call__(self, scope, receive, send):
        token = os.environ.get("MCP_BEARER_TOKEN", "").strip()
        if scope["type"] != "http" or not token:
            return self.asgi_app(scope, receive, send)

        headers = dict(scope.get("headers") or [])
        provided = headers.get(b"authorization", b"")
        expected = ("Bearer " + token).encode("utf-8", "surrogateescape")
        if hmac.compare_digest(provided, expected):
            return self.asgi_app(scope, receive, send)

        async def _reject(receive, send):
            await send(
                {
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"www-authenticate", b"Bearer"),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": b'{"error": "unauthorized"}'})

        return _reject(receive, send)


def main(transport: str = "stdio", host: str = "127.0.0.1", port: int = 8300) -> None:
    """Run the FastMCP server.

    By default uses stdio transport (for local MCP clients launched as a
    subprocess). Pass ``transport="http"`` to serve over Streamable HTTP
    for long-running / remote use; the MCP endpoint is then reachable at
    ``http://<host>:<port>/mcp``.

    When ``MCP_BEARER_TOKEN`` is set, http requests must carry the matching
    bearer token (401 otherwise); without it serving is unchanged.
    """
    if transport == "stdio":
        mcp.run()
        return

    token = os.environ.get("MCP_BEARER_TOKEN", "").strip()
    if not token:
        mcp.run(transport=transport, host=host, port=port)
        return

    import uvicorn
    from starlette.middleware import Middleware

    asgi = mcp.http_app(middleware=[Middleware(BearerAuthMiddleware)])
    uvicorn.run(asgi, host=host, port=port)


if __name__ == "__main__":
    main()

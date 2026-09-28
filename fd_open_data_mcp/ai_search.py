"""AI search: orchestrates semantic search -> graph traversal -> value query.

Phase 5 of add-entity-graph-vector-search change. Provides an `ai_search` MCP
tool that takes a natural-language query and returns structured data by
coordinating the three layers:

1. Vector search: find relevant concepts matching the query
2. Graph traversal: find related entities for those concepts
3. Value store: fetch actual values from semantic_observations
"""
from __future__ import annotations

import json

from sqlalchemy import bindparam, text

from fd_open_data_mcp import db as dbmod
from fd_open_data_mcp import vector_backend
from fd_open_data_mcp.embeddings.model import MODEL_NAME, get_model


# Lazy model singleton lives in embeddings.model (resolves by name, shared
# with semantic_search / entity search — never a machine-specific cache path).
_get_model = get_model


def ai_search(
    query: str,
    entity_type: str | None = None,
    limit: int = 20,
    include_values: bool = False,
    value_date: str | None = None,
    include_unbound: bool = False,
) -> dict:
    """Search for concepts, related entities, and optionally values using AI.

    Orchestrates a three-layer search:
    1. Semantic search: find concepts matching the natural-language query
    2. Graph traversal: find related entities for those concepts
    3. Value store: (optional) fetch actual values from semantic_observations

    Args:
        query: Natural language query (e.g., "Asian inflation indicators")
        entity_type: Filter by entity type (country, stock, industry, etc.)
        limit: Maximum number of concepts to return
        include_values: If True, fetch actual values from semantic_observations
        value_date: Date for values (e.g., "2024-01-01") - only if include_values=True
        include_unbound: If True, also return concepts with zero bindings (ranked
            after bound concepts). Default False - ai_search returns only
            retrievable concepts (spec entity-semantic-search).

    Returns:
        Dictionary with:
        - "query": the original query
        - "concepts": list of matching concepts with similarity scores
        - "entities": list of related entities (from graph traversal)
        - "values": (optional) actual values from semantic_observations
    """
    print(f"[ai_search] Query: '{query}'")
    print(f"[ai_search] Filters: entity_type={entity_type}, include_values={include_values}, include_unbound={include_unbound}")

    result = {
        "query": query,
        "entity_type": entity_type,
        "concepts": [],
        "entities": [],
        "values": [],
    }

    # Layer 1: Semantic search for concepts
    print("[ai_search] Layer 1: Semantic search for concepts...")
    concepts = _semantic_search(query, entity_type, limit, include_unbound=include_unbound)
    result["concepts"] = concepts
    print(f"[ai_search] Found {len(concepts)} concepts")

    if not concepts:
        return result

    # Layer 2: Graph traversal - find entities of the relevant type
    print("[ai_search] Layer 2: Graph traversal for entities...")
    if entity_type:
        entities = _list_entities_by_type(entity_type, limit=50)
        result["entities"] = entities
        print(f"[ai_search] Found {len(entities)} entities of type '{entity_type}'")

    # Layer 3: Value store - fetch actual values (optional)
    if include_values and concepts and entity_type:
        print("[ai_search] Layer 3: Fetching values from semantic_observations...")
        values = _fetch_values(concepts, entity_type, value_date)
        result["values"] = values
        print(f"[ai_search] Found {len(values)} value records")

    return result


def _semantic_search(query: str, entity_type: str | None, limit: int,
                     include_unbound: bool = False) -> list[dict]:
    """Perform semantic search over concepts.

    Excludes deprecated concepts (spec entity-semantic-search). Excludes
    zero-binding concepts unless ``include_unbound=True`` - ai_search returns
    only retrievable concepts by default. Ranks bound concepts ahead of unbound
    within a 0.05 similarity band.
    """
    # Load the embedding model (lazy singleton)
    model = _get_model()

    # Encode the query
    query_embedding = model.encode([query])[0]

    # Get database session
    db = dbmod.get_database()
    session = db.get_session()

    try:
        # Candidate retrieval goes through the vector backend abstraction
        # (FD_MCP_VECTOR_BACKEND: json | matrix | pgvector). The default json
        # backend is the legacy full-scan path, behavior-for-behavior.
        return vector_backend.search_concept_candidates(
            session,
            query_embedding,
            entity_type=entity_type,
            limit=limit,
            include_unbound=include_unbound,
            model=MODEL_NAME,
        )

    finally:
        session.close()


def _list_entities_by_type(entity_type: str, limit: int = 50) -> list[dict]:
    """List entities of a specific type from the entity graph."""
    db = dbmod.get_database()
    session = db.get_session()

    try:
        result = session.execute(
            text("""
                SELECT id, entity_type, code, name_en, name_zh, metadata_json
                FROM entities
                WHERE entity_type = :entity_type
                ORDER BY code
                LIMIT :limit
            """),
            {"entity_type": entity_type, "limit": limit}
        )

        entities = []
        for row in result:
            # Handle both dict (psycopg2 JSONB) and string (SQLite JSON) formats
            metadata = row.metadata_json
            if isinstance(metadata, str):
                metadata = json.loads(metadata)

            entities.append({
                "id": row.id,
                "entity_type": row.entity_type,
                "code": row.code,
                "name_en": row.name_en,
                "name_zh": row.name_zh,
                "metadata": metadata,
            })

        return entities

    finally:
        session.close()


def _fetch_values(concepts: list[dict], entity_type: str, value_date: str | None) -> list[dict]:
    """Fetch actual values from semantic_observations."""
    db = dbmod.get_database()
    session = db.get_session()

    try:
        # Get concept IDs
        concept_ids = [c["id"] for c in concepts]

        if not concept_ids:
            return []

        # Portable SQL (spec semantic-search: works on SQLite dev databases):
        # expanding IN instead of PG-only ANY(), and a ROW_NUMBER window
        # instead of PG-only DISTINCT ON.
        if value_date:
            sql = """
                SELECT so.concept_id, so.entity_type, so.entity_id, so.date, so.value, so.unit, so.source_used
                FROM semantic_observations so
                WHERE so.concept_id IN :concept_ids
                    AND so.entity_type = :entity_type
                    AND so.date = :value_date
                ORDER BY so.concept_id, so.entity_id
                LIMIT 100
            """
            params = {
                "entity_type": entity_type,
                "value_date": value_date,
            }
        else:
            # Latest value per (concept, entity) pair
            sql = """
                SELECT concept_id, entity_type, entity_id, date, value, unit, source_used
                FROM (
                    SELECT so.concept_id, so.entity_type, so.entity_id, so.date,
                           so.value, so.unit, so.source_used,
                           ROW_NUMBER() OVER (
                               PARTITION BY so.concept_id, so.entity_id
                               ORDER BY so.date DESC
                           ) AS rn
                    FROM semantic_observations so
                    WHERE so.concept_id IN :concept_ids
                        AND so.entity_type = :entity_type
                ) ranked
                WHERE rn = 1
                ORDER BY concept_id, entity_id
                LIMIT 100
            """
            params = {
                "entity_type": entity_type,
            }

        stmt = text(sql).bindparams(bindparam("concept_ids", expanding=True))
        params["concept_ids"] = concept_ids
        result = session.execute(stmt, params)

        values = []
        for row in result:
            values.append({
                "concept_id": row.concept_id,
                "entity_type": row.entity_type,
                "entity_id": row.entity_id,
                "date": row.date,
                "value": row.value,
                "unit": row.unit,
                "source_used": row.source_used,
            })

        return values

    finally:
        session.close()

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
from typing import Optional

import numpy as np
from sentence_transformers import SentenceTransformer
from sqlalchemy import text

from fd_open_data_mcp import db as dbmod
from fd_open_data_mcp.server import mcp


# Use the same model as the embedding script
MODEL_PATH = "/Users/chengsishi/.cache/huggingface/hub/models--sentence-transformers--all-MiniLM-L6-v2/snapshots/1110a243fdf4706b3f48f1d95db1a4f5529b4d41"
MODEL_NAME = "all-MiniLM-L6-v2"


@mcp.tool()
def ai_search(
    query: str,
    entity_type: str | None = None,
    limit: int = 20,
    include_values: bool = False,
    value_date: str | None = None,
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

    Returns:
        Dictionary with:
        - "query": the original query
        - "concepts": list of matching concepts with similarity scores
        - "entities": list of related entities (from graph traversal)
        - "values": (optional) actual values from semantic_observations
    """
    print(f"[ai_search] Query: '{query}'")
    print(f"[ai_search] Filters: entity_type={entity_type}, include_values={include_values}")

    result = {
        "query": query,
        "entity_type": entity_type,
        "concepts": [],
        "entities": [],
        "values": [],
    }

    # Layer 1: Semantic search for concepts
    print("[ai_search] Layer 1: Semantic search for concepts...")
    concepts = _semantic_search(query, entity_type, limit)
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


def _semantic_search(query: str, entity_type: str | None, limit: int) -> list[dict]:
    """Perform semantic search over concepts."""
    # Load the embedding model
    model = SentenceTransformer(MODEL_PATH)

    # Encode the query
    query_embedding = model.encode([query])[0]

    # Get database session
    db = dbmod.get_database()
    session = db.get_session()

    try:
        # Build the query
        filter_clauses = []
        params = {"model": MODEL_NAME}

        if entity_type:
            filter_clauses.append("c.entity_type = :entity_type")
            params["entity_type"] = entity_type

        where_clause = " AND ".join(filter_clauses) if filter_clauses else "1=1"

        sql = f"""
            SELECT
                c.id, c.code, c.name_en, c.name_zh, c.category, c.unit,
                c.measure, c.frequency, c.entity_type, c.source,
                ce.embedding
            FROM concepts c
            JOIN concept_embeddings ce ON c.id = ce.concept_id
            WHERE ce.model = :model
                AND {where_clause}
        """

        result = session.execute(text(sql), params)

        candidates = []
        for row in result:
            # Parse embedding from JSON
            embedding_str = row.embedding
            if isinstance(embedding_str, str):
                embedding_list = json.loads(embedding_str)
            else:
                embedding_list = [float(x) for x in embedding_str]

            # Compute similarity
            embedding_array = np.array(embedding_list, dtype=np.float32)
            query_array = np.array(query_embedding, dtype=np.float32)
            similarity = float(np.dot(query_array, embedding_array))

            candidates.append({
                "id": row.id,
                "code": row.code,
                "name_en": row.name_en,
                "name_zh": row.name_zh,
                "category": row.category,
                "unit": row.unit,
                "measure": row.measure,
                "frequency": row.frequency,
                "entity_type": row.entity_type,
                "source": row.source,
                "similarity": similarity,
            })

        # Sort by similarity and take top K
        candidates.sort(key=lambda x: x["similarity"], reverse=True)
        return candidates[:limit]

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

        # Build the query
        if value_date:
            sql = """
                SELECT so.concept_id, so.entity_type, so.entity_id, so.date, so.value, so.unit, so.source_used
                FROM semantic_observations so
                WHERE so.concept_id = ANY(:concept_ids)
                    AND so.entity_type = :entity_type
                    AND so.date = :value_date
                ORDER BY so.concept_id, so.entity_id
                LIMIT 100
            """
            params = {
                "concept_ids": concept_ids,
                "entity_type": entity_type,
                "value_date": value_date,
            }
        else:
            # Get the latest values for each (concept, entity) pair
            sql = """
                SELECT DISTINCT ON (so.concept_id, so.entity_id)
                    so.concept_id, so.entity_type, so.entity_id, so.date, so.value, so.unit, so.source_used
                FROM semantic_observations so
                WHERE so.concept_id = ANY(:concept_ids)
                    AND so.entity_type = :entity_type
                ORDER BY so.concept_id, so.entity_id, so.date DESC
                LIMIT 100
            """
            params = {
                "concept_ids": concept_ids,
                "entity_type": entity_type,
            }

        result = session.execute(text(sql), params)

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

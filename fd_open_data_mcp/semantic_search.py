"""Semantic search for concepts using vector embeddings.

Implements cosine similarity search over concept embeddings to find semantically
similar concepts based on natural language queries.
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
def semantic_search(
    query: str,
    entity_type: str | None = None,
    frequency: str | None = None,
    limit: int = 20,
) -> list[dict]:
    """Search concepts semantically using vector embeddings.

    Args:
        query: Natural language query (e.g., "inflation indicators", "stock price history")
        entity_type: Filter by entity type (country, stock, industry, etc.) - optional
        frequency: Filter by frequency (daily, weekly, monthly, yearly, irregular) - optional
        limit: Maximum number of results to return

    Returns:
        List of matching concepts with scores, ordered by similarity
    """
    # Load the embedding model
    model = SentenceTransformer(MODEL_PATH)
    embedding_dim = model.get_embedding_dimension()

    # Get database session
    db = dbmod.get_database()
    session = db.get_session()

    try:
        # Encode the query
        query_embedding = model.encode([query])[0]

        print(f"[semantic_search] Query: '{query}'")
        print(f"[semantic_search] Filters: entity_type={entity_type}, frequency={frequency}")

        # Simple approach: fetch all relevant concepts and their embeddings
        # Then compute similarity locally
        filter_clauses = []
        params = {"model": MODEL_NAME}

        if entity_type:
            filter_clauses.append("c.entity_type = :entity_type")
            params["entity_type"] = entity_type

        if frequency:
            filter_clauses.append("c.frequency = :frequency")
            params["frequency"] = frequency

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

            # Normalize and compute similarity
            embedding_array = np.array(embedding_list, dtype=np.float32)
            query_array = np.array(query_embedding, dtype=np.float32)

            # Cosine similarity for normalized vectors = dot product
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
        results = candidates[:limit]

        print(f"[semantic_search] Found {len(results)} results")
        for r in results[:5]:
            print(f"  [{r['similarity']:.3f}] {r['code']}: {r['name_en']}")

        return results

    except Exception as e:
        print(f"[semantic_search] Error: {e}")
        raise
    finally:
        session.close()


@mcp.tool()
def re_embed_concept(concept_id: int) -> dict:
    """Re-embed a single concept.

    Args:
        concept_id: ID of the concept to re-embed

    Returns:
        Result dictionary with status
    """
    # Load the embedding model
    model = SentenceTransformer(MODEL_PATH)

    # Get database session
    db = dbmod.get_database()
    session = db.get_session()

    try:
        # Get the concept
        result = session.execute(text("""
            SELECT id, code, name_en, name_zh, category, unit, measure, frequency, entity_type
            FROM concepts
            WHERE id = :concept_id
        """), {"concept_id": concept_id}).first()

        if not result:
            return {"status": "error", "message": f"Concept {concept_id} not found"}

        # Create text representation
        parts = []
        if result.name_en:
            parts.append(result.name_en)
        if result.name_zh:
            parts.append(result.name_zh)
        if result.category:
            parts.append(f"category: {result.category}")
        if result.unit:
            parts.append(f"unit: {result.unit}")
        if result.measure:
            parts.append(f"measure: {result.measure}")
        if result.frequency:
            parts.append(f"frequency: {result.frequency}")
        if result.entity_type:
            parts.append(f"entity_type: {result.entity_type}")
        parts.append(f"code: {result.code}")

        text = " | ".join(parts)

        # Generate embedding
        embedding = model.encode([text])[0]
        embedding_list = embedding.tolist()

        # Check if embedding exists
        existing = session.execute(
            text("SELECT id FROM concept_embeddings WHERE concept_id = :concept_id AND model = :model"),
            {"concept_id": concept_id, "model": MODEL_NAME}
        ).first()

        if existing:
            # Update
            session.execute(
                text("""
                    UPDATE concept_embeddings
                    SET embedding = :embedding
                    WHERE concept_id = :concept_id AND model = :model
                """),
                {
                    "embedding": json.dumps(embedding_list),
                    "concept_id": concept_id,
                    "model": MODEL_NAME,
                }
            )
            status = "updated"
        else:
            # Insert
            session.execute(
                text("""
                    INSERT INTO concept_embeddings (concept_id, embedding, model)
                    VALUES (:concept_id, :embedding, :model)
                """),
                {
                    "concept_id": concept_id,
                    "embedding": json.dumps(embedding_list),
                    "model": MODEL_NAME,
                }
            )
            status = "inserted"

        session.commit()

        return {
            "status": status,
            "concept_id": concept_id,
            "code": result.code,
            "embedding_dimension": len(embedding_list),
        }

    except Exception as e:
        session.rollback()
        return {"status": "error", "message": str(e)}
    finally:
        session.close()

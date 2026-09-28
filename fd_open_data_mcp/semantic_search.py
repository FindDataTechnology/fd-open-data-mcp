"""Semantic search for concepts using vector embeddings.

Implements cosine similarity search over concept embeddings to find semantically
similar concepts based on natural language queries.
"""
from __future__ import annotations

import json

from sqlalchemy import text

from fd_open_data_mcp import db as dbmod
from fd_open_data_mcp import vector_backend
from fd_open_data_mcp.embeddings.model import MODEL_NAME, get_model

# Lazy model singleton lives in embeddings.model (resolves by name, shared
# with ai_search / entity search — never a machine-specific cache path).
_get_model = get_model


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
    # Load the embedding model (lazy singleton)
    model = _get_model()
    embedding_dim = model.get_embedding_dimension()

    # Get database session
    db = dbmod.get_database()
    session = db.get_session()

    try:
        # Encode the query
        query_embedding = model.encode([query])[0]

        print(f"[semantic_search] Query: '{query}'")
        print(f"[semantic_search] Filters: entity_type={entity_type}, frequency={frequency}")

        # Candidate retrieval goes through the vector backend abstraction
        # (FD_MCP_VECTOR_BACKEND: json | matrix | pgvector). The default json
        # backend is the legacy full-scan path, behavior-for-behavior.
        results = vector_backend.search_concept_candidates(
            session,
            query_embedding,
            entity_type=entity_type,
            limit=limit,
            include_unbound=True,
            model=MODEL_NAME,
            frequency=frequency,
        )

        print(f"[semantic_search] Found {len(results)} results")
        for r in results[:5]:
            print(f"  [{r['similarity']:.3f}] {r['code']}: {r['name_en']}")

        return results

    except Exception as e:
        print(f"[semantic_search] Error: {e}")
        raise
    finally:
        session.close()


def re_embed_concept(concept_id: int) -> dict:
    """Re-embed a single concept.

    Args:
        concept_id: ID of the concept to re-embed

    Returns:
        Result dictionary with status
    """
    # Load the embedding model (lazy singleton)
    model = _get_model()

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

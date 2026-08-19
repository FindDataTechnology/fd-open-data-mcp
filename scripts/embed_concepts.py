"""Embed all concepts for semantic search.

Phase 3 of add-entity-graph-vector-search change. Generates vector embeddings
for all concepts using sentence-transformers and stores them in concept_embeddings.

Usage: python scripts/embed_concepts.py
"""
from __future__ import annotations

import json
import os
from sqlalchemy import text
from sentence_transformers import SentenceTransformer
from fd_open_data_mcp import db as dbmod

# Force remote Postgres connection (canonical: guangzhou-xinru:30432; from the
# Mac via tunnel: ssh -N -L 30432:127.0.0.1:30432 ubuntu@134.175.46.69)
DATABASE_URL = "postgresql://fd:FD_PG_PASSWORD@127.0.0.1:30432/fd_open_data"
os.environ["FD_OPEN_DATA_MCP_DATABASE_URL"] = DATABASE_URL

# Load model from local cache (avoids network issues)
MODEL_PATH = "/Users/chengsishi/.cache/huggingface/hub/models--sentence-transformers--all-MiniLM-L6-v2/snapshots/1110a243fdf4706b3f48f1d95db1a4f5529b4d41"
MODEL_NAME = "all-MiniLM-L6-v2"


def main():
    """Embed all concepts."""
    print("=== Concept Embedding ===\n")

    # Load the embedding model from local cache
    print(f"Loading embedding model from {MODEL_PATH}")
    try:
        model = SentenceTransformer(MODEL_PATH)
    except Exception as e:
        print(f"Failed to load model from cache, trying online download: {e}")
        print("Note: This requires internet access or proxy configuration.")
        raise

    print(f"  Model loaded. Embedding dimension: {model.get_sentence_embedding_dimension()}\n")

    # Get database session
    db = dbmod.get_database()
    session = db.get_session()

    try:
        # Get all concepts
        print("Fetching concepts from database...")
        result = session.execute(text("""
            SELECT id, code, name_en, name_zh, category, unit, measure, frequency, entity_type
            FROM concepts
        """))

        concepts = []
        for row in result:
            concepts.append({
                "id": row.id,
                "code": row.code,
                "name_en": row.name_en or "",
                "name_zh": row.name_zh or "",
                "category": row.category or "",
                "unit": row.unit or "",
                "measure": row.measure or "",
                "frequency": row.frequency or "",
                "entity_type": row.entity_type or "",
            })

        print(f"  Found {len(concepts)} concepts\n")

        # Generate embeddings
        print("Generating embeddings...")
        texts = []
        for c in concepts:
            # Create a rich text representation for embedding
            # Include all available metadata to capture semantic meaning
            parts = []
            if c["name_en"]:
                parts.append(c["name_en"])
            if c["name_zh"]:
                parts.append(c["name_zh"])
            if c["category"]:
                parts.append(f"category: {c['category']}")
            if c["unit"]:
                parts.append(f"unit: {c['unit']}")
            if c["measure"]:
                parts.append(f"measure: {c['measure']}")
            if c["frequency"]:
                parts.append(f"frequency: {c['frequency']}")
            if c["entity_type"]:
                parts.append(f"entity_type: {c['entity_type']}")
            parts.append(f"code: {c['code']}")

            input_text = " | ".join(parts)
            texts.append(input_text)

        # Generate embeddings in batches
        print(f"  Generating {len(texts)} embeddings...")
        embeddings = model.encode(texts, show_progress_bar=True, batch_size=32)
        print(f"  Generated {len(embeddings)} embeddings\n")

        # Store embeddings in database
        print("Storing embeddings in database...")
        inserted = 0
        updated = 0

        for concept, embedding in zip(concepts, embeddings):
            embedding_list = embedding.tolist()

            # Check if embedding already exists
            existing = session.execute(
                text("SELECT id FROM concept_embeddings WHERE concept_id = :concept_id AND model = :model"),
                {"concept_id": concept["id"], "model": MODEL_NAME}
            ).first()

            if existing:
                # Update existing embedding
                session.execute(
                    text("""
                        UPDATE concept_embeddings
                        SET embedding = :embedding
                        WHERE concept_id = :concept_id AND model = :model
                    """),
                    {
                        "embedding": json.dumps(embedding_list),
                        "concept_id": concept["id"],
                        "model": MODEL_NAME,
                    }
                )
                updated += 1
            else:
                # Insert new embedding
                session.execute(
                    text("""
                        INSERT INTO concept_embeddings (concept_id, embedding, model)
                        VALUES (:concept_id, :embedding, :model)
                    """),
                    {
                        "concept_id": concept["id"],
                        "embedding": json.dumps(embedding_list),
                        "model": MODEL_NAME,
                    }
                )
                inserted += 1

        session.commit()

        print(f"\n=== Embedding Complete ===")
        print(f"  Inserted: {inserted}")
        print(f"  Updated: {updated}")
        print(f"  Total: {inserted + updated}")
        print(f"\nYou can now use the semantic_search MCP tool to search concepts.")

    except Exception as e:
        session.rollback()
        print(f"\nError during embedding: {e}")
        raise
    finally:
        session.close()


if __name__ == "__main__":
    main()

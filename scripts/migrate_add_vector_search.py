"""Migration: Add vector search support for semantic concept search.

Phase 3 of add-entity-graph-vector-search change. Adds concept_embeddings table
to store vector embeddings of concept descriptions for semantic search.

Since pgvector is not available, we store embeddings as JSON arrays and implement
cosine similarity search in Python using sentence-transformers.

Usage: python scripts/migrate_add_vector_search.py
"""
from __future__ import annotations

import os
from sqlalchemy import text
from fd_open_data_mcp import db as dbmod

# Force remote Postgres connection
DATABASE_URL = "postgresql://admin:admin123@192.168.1.4:5433/postgres"
os.environ["FD_OPEN_DATA_MCP_DATABASE_URL"] = DATABASE_URL


def main():
    """Run the vector search migration."""
    print("=== Vector Search Migration ===\n")

    # Get database session
    db = dbmod.get_database()
    session = db.get_session()

    try:
        # Create concept_embeddings table
        print("Creating 'concept_embeddings' table...")
        session.execute(text("""
            CREATE TABLE IF NOT EXISTS concept_embeddings (
                id SERIAL PRIMARY KEY,
                concept_id INTEGER NOT NULL REFERENCES concepts(id) ON DELETE CASCADE,
                embedding JSONB NOT NULL,
                model VARCHAR(128) NOT NULL,
                created_at TIMESTAMP DEFAULT NOW(),
                UNIQUE(concept_id, model)
            )
        """))

        # Create index on concept_id
        print("Creating indexes on 'concept_embeddings'...")
        session.execute(text("""
            CREATE INDEX IF NOT EXISTS idx_concept_embeddings_concept_id
            ON concept_embeddings(concept_id)
        """))

        session.commit()

        print("\n=== Migration Complete ===\n")
        print("New tables:")
        print("  - concept_embeddings (concept_id, embedding, model, created_at)")
        print("\nNext steps:")
        print("  1. Install sentence-transformers: pip install sentence-transformers")
        print("  2. Run: python scripts/embed_concepts.py")
        print("  3. Use semantic_search MCP tool to search concepts")

    except Exception as e:
        session.rollback()
        print(f"\nError during migration: {e}")
        raise
    finally:
        session.close()


if __name__ == "__main__":
    main()

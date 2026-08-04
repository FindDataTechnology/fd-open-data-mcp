"""Migration: Add entity graph tables (entities + entity_relationships).

Phase 2 of add-entity-graph-vector-search change. Adds two new tables to the
fd-open-data-mcp ontology database:

1. `entities` - unified registry for all entity types
2. `entity_relationships` - edges between entities with temporal validity

Usage: python scripts/migrate_add_entity_graph.py
"""
from __future__ import annotations

import os
from datetime import datetime, timezone

from sqlalchemy import text
from fd_open_data_mcp import db as dbmod

# Force remote Postgres connection
DATABASE_URL = "postgresql://admin:admin123@192.168.1.4:5433/postgres"
os.environ["FD_OPEN_DATA_MCP_DATABASE_URL"] = DATABASE_URL


def main():
    """Run the entity graph migration."""
    print("=== Entity Graph Migration ===\n")

    # Reset/get fresh database session
    db = dbmod.get_database()
    s = db.get_session()

    try:
        # Create entities table
        print("Creating 'entities' table...")
        s.execute(text("""
            CREATE TABLE IF NOT EXISTS entities (
                id SERIAL PRIMARY KEY,
                entity_type VARCHAR(32) NOT NULL,
                code VARCHAR(128) NOT NULL,
                name_en VARCHAR(255),
                name_zh VARCHAR(255),
                metadata_json JSONB,
                updated_at TIMESTAMP DEFAULT NOW(),
                UNIQUE(entity_type, code)
            )
        """))

        # Create indexes on entities
        print("Creating indexes on 'entities'...")
        s.execute(text("""
            CREATE INDEX IF NOT EXISTS idx_entities_type ON entities(entity_type)
        """))
        # Note: GIN index on jsonb is optional for now; can be added later if needed
        # s.execute(text("""
        #     CREATE INDEX IF NOT EXISTS idx_entities_metadata ON entities USING GIN(metadata_json jsonb_ops)
        #     WHERE metadata_json IS NOT NULL
        # """))

        # Create entity_relationships table
        print("Creating 'entity_relationships' table...")
        s.execute(text("""
            CREATE TABLE IF NOT EXISTS entity_relationships (
                id SERIAL PRIMARY KEY,
                source_id INTEGER REFERENCES entities(id) ON DELETE CASCADE,
                relation_type VARCHAR(64) NOT NULL,
                target_id INTEGER REFERENCES entities(id) ON DELETE CASCADE,
                valid_from TIMESTAMP,
                valid_to TIMESTAMP,
                metadata_json JSONB,
                created_at TIMESTAMP DEFAULT NOW(),
                UNIQUE(source_id, relation_type, target_id, valid_from)
            )
        """))

        # Create indexes on relationships
        print("Creating indexes on 'entity_relationships'...")
        s.execute(text("""
            CREATE INDEX IF NOT EXISTS idx_rel_source ON entity_relationships(source_id, relation_type)
        """))
        s.execute(text("""
            CREATE INDEX IF NOT EXISTS idx_rel_target ON entity_relationships(target_id, relation_type)
        """))

        # Update model class docstring in comments
        print("\nUpdating models.py docstring...")
        # Note: this is just informational; the models were already added manually above

        s.commit()
        print("\n=== Migration Complete ===\n")
        print("New tables:")
        print("  - entities (id, entity_type, code, name_en, name_zh, metadata, updated_at)")
        print("  - entity_relationships (source_id, relation_type, target_id, valid_from, valid_to, metadata)")

    except Exception as e:
        s.rollback()
        print(f"\nError during migration: {e}")
        raise
    finally:
        s.close()


if __name__ == "__main__":
    main()

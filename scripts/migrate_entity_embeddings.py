"""Migration script to create entity_embeddings table."""
import os
import sys
from pathlib import Path

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker


def migrate_entity_embeddings_table():
    """Create entity_embeddings table for storing entity vector embeddings."""

    # Get database URL from environment or use default
    database_url = os.environ.get(
        "FD_OPEN_DATA_MCP_DATABASE_URL",
        "postgresql://fd:FD_PG_PASSWORD@guangzhou-xinru:30432/fd_open_data"
    )

    print(f"Connecting to database: {database_url.split('@')[1] if '@' in database_url else 'local'}")

    engine = create_engine(database_url)
    Session = sessionmaker(bind=engine)
    session = Session()

    try:
        # Check if table already exists
        result = session.execute(text("""
            SELECT EXISTS (
                SELECT FROM information_schema.tables
                WHERE table_name = 'entity_embeddings'
            )
        """))

        exists = result.scalar()

        if exists:
            print("✓ entity_embeddings table already exists")
            return

        # Create entity_embeddings table
        print("Creating entity_embeddings table...")

        session.execute(text("""
            CREATE TABLE entity_embeddings (
                id SERIAL PRIMARY KEY,
                entity_id INTEGER NOT NULL REFERENCES entities(id) ON DELETE CASCADE,
                embedding TEXT NOT NULL,
                model VARCHAR(128) NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(entity_id, model)
            )
        """))

        # Create index for faster similarity search
        print("Creating index on entity_embeddings...")
        session.execute(text("""
            CREATE INDEX idx_entity_embeddings_entity_id
            ON entity_embeddings(entity_id)
        """))

        session.execute(text("""
            CREATE INDEX idx_entity_embeddings_model
            ON entity_embeddings(model)
        """))

        session.commit()

        print("✓ entity_embeddings table created successfully")
        print("  - entity_id: Foreign key to entities table")
        print("  - embedding: JSON array of floats (384 dimensions)")
        print("  - model: Model name (e.g., 'all-MiniLM-L6-v2')")
        print("  - Indexes on entity_id and model for fast lookup")

    except Exception as e:
        session.rollback()
        print(f"✗ Migration failed: {e}")
        raise
    finally:
        session.close()


if __name__ == "__main__":
    migrate_entity_embeddings_table()

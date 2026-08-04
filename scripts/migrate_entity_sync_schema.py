"""
Migration script for entity sync mechanism.
Creates entity_sources, entity_sync_logs, and entity_sync_schedules tables.
"""
import os
from pathlib import Path
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

# Database connection
DATABASE_URL = os.environ.get(
    "FD_OPEN_DATA_MCP_DATABASE_URL",
    "postgresql://admin:admin123@192.168.1.4:5433/postgres"
)

def migrate():
    """Run database migrations for entity sync tables."""
    print("Starting entity sync schema migration...")

    engine = create_engine(DATABASE_URL)
    Session = sessionmaker(bind=engine)
    session = Session()

    try:
        # Task 1.1: Create entity_sources table
        print("\n[1/6] Creating entity_sources table...")
        session.execute(text("""
            CREATE TABLE IF NOT EXISTS entity_sources (
                id SERIAL PRIMARY KEY,
                entity_type VARCHAR(32) NOT NULL UNIQUE,
                source_table VARCHAR(64) NOT NULL,
                source_schema VARCHAR(64) DEFAULT 'public',
                code_column VARCHAR(64) NOT NULL,
                name_en_column VARCHAR(64),
                name_zh_column VARCHAR(64),
                select_filter TEXT,
                metadata_columns TEXT[],
                enabled BOOLEAN DEFAULT TRUE,
                last_sync_at TIMESTAMP,
                updated_at TIMESTAMP DEFAULT NOW()
            )
        """))
        print("✓ entity_sources table created")

        # Task 1.2: Create entity_sync_logs table
        print("\n[2/6] Creating entity_sync_logs table...")
        session.execute(text("""
            CREATE TABLE IF NOT EXISTS entity_sync_logs (
                id SERIAL PRIMARY KEY,
                entity_type VARCHAR(32) NOT NULL,
                started_at TIMESTAMP NOT NULL,
                finished_at TIMESTAMP,
                inserted_count INTEGER DEFAULT 0,
                updated_count INTEGER DEFAULT 0,
                deleted_count INTEGER DEFAULT 0,
                error_count INTEGER DEFAULT 0,
                status VARCHAR(16) NOT NULL,
                error_message TEXT,
                duration_seconds INTEGER,
                scheduler_id INTEGER
            )
        """))
        print("✓ entity_sync_logs table created")

        # Task 1.3: Create entity_sync_schedules table
        print("\n[3/6] Creating entity_sync_schedules table...")
        session.execute(text("""
            CREATE TABLE IF NOT EXISTS entity_sync_schedules (
                id SERIAL PRIMARY KEY,
                entity_type VARCHAR(32) NOT NULL UNIQUE,
                schedule_type VARCHAR(16) NOT NULL,
                cron_expr VARCHAR(64),
                interval_minutes INTEGER,
                timezone VARCHAR(64) DEFAULT 'UTC',
                enabled BOOLEAN DEFAULT TRUE,
                next_run_at TIMESTAMP NOT NULL,
                last_run_at TIMESTAMP,
                created_at TIMESTAMP DEFAULT NOW()
            )
        """))
        print("✓ entity_sync_schedules table created")

        # Task 1.4: Add indexes
        print("\n[4/6] Creating indexes...")
        session.execute(text("""
            CREATE INDEX IF NOT EXISTS idx_entity_sources_entity_type
            ON entity_sources(entity_type)
        """))
        session.execute(text("""
            CREATE INDEX IF NOT EXISTS idx_entity_sync_logs_started_at
            ON entity_sync_logs(started_at DESC)
        """))
        session.execute(text("""
            CREATE INDEX IF NOT EXISTS idx_entity_sync_logs_entity_type
            ON entity_sync_logs(entity_type)
        """))
        session.execute(text("""
            CREATE INDEX IF NOT EXISTS idx_entity_sync_schedules_next_run
            ON entity_sync_schedules(next_run_at)
            WHERE enabled = TRUE
        """))
        print("✓ Indexes created")

        # Task 1.5: Seed initial configuration for 10 entity types
        print("\n[5/6] Seeding initial entity source configurations...")

        initial_sources = [
            {
                "entity_type": "country",
                "source_table": "countries",
                "code_column": "iso_code",
                "name_en_column": "name_en",
                "name_zh_column": "name_zh",
                "metadata_columns": ["region"]
            },
            {
                "entity_type": "city",
                "source_table": "cities",
                "code_column": "code",
                "name_en_column": "name_en",
                "name_zh_column": "name_zh",
                "metadata_columns": ["country_iso", "latitude", "longitude"]
            },
            {
                "entity_type": "company",
                "source_table": "companies",
                "code_column": "code",
                "name_en_column": "name_en",
                "name_zh_column": "name_zh",
                "metadata_columns": ["sector"]
            },
            {
                "entity_type": "stock",
                "source_table": "symbols",
                "code_column": "ticker",
                "name_en_column": "name_en",
                "name_zh_column": "name_zh",
                "select_filter": "symbol_type = 'stock'",
                "metadata_columns": ["exchange", "symbol_type", "company_code"]
            },
            {
                "entity_type": "index",
                "source_table": "symbols",
                "code_column": "ticker",
                "name_en_column": "name_en",
                "name_zh_column": "name_zh",
                "select_filter": "symbol_type = 'index'",
                "metadata_columns": ["exchange", "symbol_type"]
            },
            {
                "entity_type": "etf",
                "source_table": "symbols",
                "code_column": "ticker",
                "name_en_column": "name_en",
                "name_zh_column": "name_zh",
                "select_filter": "symbol_type = 'etf'",
                "metadata_columns": ["exchange", "symbol_type"]
            },
            {
                "entity_type": "crypto",
                "source_table": "symbols",
                "code_column": "ticker",
                "name_en_column": "name_en",
                "name_zh_column": "name_zh",
                "select_filter": "symbol_type = 'coin'",
                "metadata_columns": ["exchange", "symbol_type"]
            },
            {
                "entity_type": "future",
                "source_table": "symbols",
                "code_column": "ticker",
                "name_en_column": "name_en",
                "name_zh_column": "name_zh",
                "select_filter": "symbol_type = 'future'",
                "metadata_columns": ["exchange", "symbol_type"]
            },
            {
                "entity_type": "bond",
                "source_table": "symbols",
                "code_column": "ticker",
                "name_en_column": "name_en",
                "name_zh_column": "name_zh",
                "select_filter": "symbol_type = 'bond'",
                "metadata_columns": ["exchange", "symbol_type"]
            },
            {
                "entity_type": "industry",
                "source_table": "sw_industries",
                "code_column": "code",
                "name_en_column": "name_en",
                "name_zh_column": "name_zh",
                "metadata_columns": ["level", "parent_code"]
            }
        ]

        for source in initial_sources:
            session.execute(text("""
                INSERT INTO entity_sources (
                    entity_type, source_table, code_column,
                    name_en_column, name_zh_column, select_filter,
                    metadata_columns
                ) VALUES (
                    :entity_type, :source_table, :code_column,
                    :name_en_column, :name_zh_column, :select_filter,
                    :metadata_columns
                )
                ON CONFLICT (entity_type) DO NOTHING
            """), {
                "entity_type": source["entity_type"],
                "source_table": source["source_table"],
                "code_column": source["code_column"],
                "name_en_column": source.get("name_en_column"),
                "name_zh_column": source.get("name_zh_column"),
                "select_filter": source.get("select_filter"),
                "metadata_columns": source.get("metadata_columns", [])
            })

        print("✓ Seeded 10 entity source configurations")

        # Task 1.6: Commit transaction
        print("\n[6/6] Committing migration...")
        session.commit()
        print("✓ Migration committed successfully")

        print("\n" + "="*60)
        print("Migration completed successfully!")
        print("="*60)
        print("\nCreated tables:")
        print("  - entity_sources (10 initial configurations)")
        print("  - entity_sync_logs")
        print("  - entity_sync_schedules")
        print("\nNext steps:")
        print("  1. Review configurations: SELECT * FROM entity_sources;")
        print("  2. Implement sync engine (Phase 2)")
        print("  3. Add MCP tools (Phase 4)")

    except Exception as e:
        session.rollback()
        print(f"\n❌ Migration failed: {e}")
        raise
    finally:
        session.close()

if __name__ == "__main__":
    migrate()

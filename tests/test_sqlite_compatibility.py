"""
Tests for SQLite compatibility in entity sync system.
Verifies that sync engine works with both PostgreSQL and SQLite.
"""
import os
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from fd_open_data_mcp.db.adapters import get_adapter, PostgreSQLAdapter, SQLiteAdapter
from fd_open_data_mcp.sync.locks import get_lock_manager, AdvisoryLockManager, FileLockManager


class TestDatabaseAdapters:
    """Test database adapter functionality."""

    def test_postgresql_adapter_creation(self):
        """Test PostgreSQL adapter creation."""
        adapter = get_adapter("postgresql://user:pass@localhost/db")
        assert isinstance(adapter, PostgreSQLAdapter)
        assert adapter.get_json_type() == "JSONB"
        assert adapter.get_batch_size() == 500

    def test_sqlite_adapter_creation(self):
        """Test SQLite adapter creation."""
        adapter = get_adapter("sqlite:///test.db")
        assert isinstance(adapter, SQLiteAdapter)
        assert adapter.get_json_type() == "TEXT"
        assert adapter.get_batch_size() == 100

    def test_json_serialization_postgresql(self):
        """Test JSON serialization for PostgreSQL."""
        adapter = PostgreSQLAdapter("postgresql://localhost/db")
        data = {"key": "value", "number": 42}
        serialized = adapter.serialize_json(data)
        assert serialized == '{"key": "value", "number": 42}'
        deserialized = adapter.deserialize_json(serialized)
        assert deserialized == data

    def test_json_serialization_sqlite(self):
        """Test JSON serialization for SQLite."""
        adapter = SQLiteAdapter("sqlite:///test.db")
        data = {"key": "value", "number": 42}
        serialized = adapter.serialize_json(data)
        assert serialized == '{"key": "value", "number": 42}'
        deserialized = adapter.deserialize_json(serialized)
        assert deserialized == data


class TestLockManagers:
    """Test lock manager functionality."""

    def test_postgresql_lock_manager_creation(self):
        """Test PostgreSQL lock manager creation."""
        lock_manager = get_lock_manager("postgresql://localhost/db")
        assert isinstance(lock_manager, AdvisoryLockManager)

    def test_sqlite_lock_manager_creation(self):
        """Test SQLite lock manager creation."""
        lock_manager = get_lock_manager("sqlite:///test.db")
        assert isinstance(lock_manager, FileLockManager)

    def test_file_lock_manager_with_custom_dir(self):
        """Test file lock manager with custom directory."""
        from pathlib import Path
        lock_manager = get_lock_manager(
            "sqlite:///test.db",
            lock_dir="/tmp/custom_locks"
        )
        assert isinstance(lock_manager, FileLockManager)
        assert str(lock_manager.lock_dir) == "/tmp/custom_locks"


class TestSQLiteMigration:
    """Test SQLite migration script."""

    def test_sqlite_migration_creates_tables(self):
        """Test that SQLite migration creates all required tables."""
        import subprocess
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            db_url = f"sqlite:///{db_path}"

            # Run migration script
            result = subprocess.run(
                ["uv", "run", "python", "scripts/migrate_entity_sync_schema_universal.py", db_url],
                capture_output=True,
                text=True,
                cwd="/Users/chengsishi/finddata/fd-open-data-mcp"
            )

            assert result.returncode == 0
            assert "Migration completed successfully" in result.stdout

            # Verify tables exist
            engine = create_engine(db_url)
            with engine.connect() as conn:
                result = conn.execute(text("""
                    SELECT name FROM sqlite_master
                    WHERE type='table' AND name IN ('entity_sources', 'entity_sync_logs', 'entity_sync_schedules')
                """))
                tables = [row[0] for row in result]
                assert "entity_sources" in tables
                assert "entity_sync_logs" in tables
                assert "entity_sync_schedules" in tables


class TestSyncEngineSQLite:
    """Test sync engine with SQLite database."""

    @pytest.fixture
    def sqlite_db(self):
        """Create a temporary SQLite database for testing."""
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "test.db")
            db_url = f"sqlite:///{db_path}"

            # Run migration
            engine = create_engine(db_url)
            with engine.connect() as conn:
                conn.execute(text("""
                    CREATE TABLE entity_sources (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        entity_type VARCHAR(32) NOT NULL UNIQUE,
                        source_table VARCHAR(64) NOT NULL,
                        source_schema VARCHAR(64) DEFAULT 'public',
                        code_column VARCHAR(64) NOT NULL,
                        name_en_column VARCHAR(64),
                        name_zh_column VARCHAR(64),
                        select_filter TEXT,
                        metadata_columns TEXT,
                        enabled BOOLEAN DEFAULT 1,
                        last_sync_at TEXT,
                        updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                    )
                """))
                conn.execute(text("""
                    CREATE TABLE entity_sync_logs (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        entity_type VARCHAR(32) NOT NULL,
                        started_at TEXT NOT NULL,
                        finished_at TEXT,
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
                conn.execute(text("""
                    CREATE TABLE entities (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        entity_type VARCHAR(32) NOT NULL,
                        code VARCHAR(128) NOT NULL,
                        name_en VARCHAR(255),
                        name_zh VARCHAR(255),
                        metadata_json TEXT,
                        updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                        UNIQUE(entity_type, code)
                    )
                """))
                conn.commit()

            yield db_url

    def test_sync_engine_initialization_sqlite(self, sqlite_db):
        """Test sync engine initialization with SQLite."""
        from fd_open_data_mcp.sync.engine import EntitySyncEngine

        engine = EntitySyncEngine(sqlite_db)
        assert isinstance(engine.adapter, SQLiteAdapter)
        assert isinstance(engine.lock_manager, FileLockManager)

    def test_batch_insert_sqlite(self, sqlite_db):
        """Test batch insert with SQLite."""
        from fd_open_data_mcp.sync.engine import EntitySyncEngine

        engine = EntitySyncEngine(sqlite_db)
        entities = [
            {"code": "TEST1", "name_en": "Test 1", "name_zh": "测试1", "metadata_json": {"key": "value1"}},
            {"code": "TEST2", "name_en": "Test 2", "name_zh": "测试2", "metadata_json": {"key": "value2"}},
        ]

        inserted = engine.batch_insert_entities("test_type", entities)
        assert inserted == 2

        # Verify entities were inserted
        db_engine = create_engine(sqlite_db)
        with db_engine.connect() as conn:
            result = conn.execute(text("SELECT COUNT(*) FROM entities WHERE entity_type = 'test_type'"))
            count = result.scalar()
            assert count == 2


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

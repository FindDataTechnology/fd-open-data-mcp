# SQLite Compatibility Guide

This guide explains how to use the entity sync system with SQLite databases, which is useful for local development, testing, and small-scale deployments.

## Overview

The entity sync system now supports both PostgreSQL and SQLite databases through an adapter pattern. The system automatically detects the database type and uses the appropriate implementation for:

- JSON storage (JSONB for PostgreSQL, TEXT for SQLite)
- Locking mechanisms (Advisory Locks for PostgreSQL, File Locks for SQLite)
- Batch operations (500 rows for PostgreSQL, 100 rows for SQLite)
- Schema creation (SERIAL for PostgreSQL, AUTOINCREMENT for SQLite)

## When to Use SQLite

**Use SQLite for:**
- Local development and testing
- Small projects (< 1000 entities)
- Quick prototyping
- Offline development

**Use PostgreSQL for:**
- Production environments
- Large datasets (> 1000 entities)
- High concurrency requirements
- Advanced JSON querying

## Configuration

### Database URL

Set the `FD_OPEN_DATA_MCP_DATABASE_URL` environment variable:

```bash
# PostgreSQL (production)
export FD_OPEN_DATA_MCP_DATABASE_URL="postgresql://user:pass@localhost:5432/dbname"

# SQLite (development)
export FD_OPEN_DATA_MCP_DATABASE_URL="sqlite:///path/to/database.db"
```

### Lock Configuration (SQLite only)

For SQLite environments, you can configure file-based locks:

```bash
# Lock directory (default: /tmp/entity_sync_lock)
export SYNC_LOCK_DIR="/var/run/sync-locks"

# Lock timeout in seconds (default: 60)
export SYNC_LOCK_TIMEOUT=30
```

## Migration

### Running Migrations

The migration script automatically detects the database type and creates appropriate schema:

```bash
# Using environment variable
uv run python scripts/migrate_entity_sync_schema_universal.py

# Using command-line argument
uv run python scripts/migrate_entity_sync_schema_universal.py "sqlite:///test.db"
```

### Schema Differences

| Feature | PostgreSQL | SQLite |
|---------|-----------|--------|
| Auto-increment | `SERIAL PRIMARY KEY` | `INTEGER PRIMARY KEY AUTOINCREMENT` |
| JSON storage | `JSONB` | `TEXT` |
| Timestamps | `TIMESTAMP WITH TIME ZONE` | `TEXT` (ISO 8601) |
| Upsert | `ON CONFLICT ... DO UPDATE` | `INSERT OR REPLACE` |

## Usage

### Sync Engine

The sync engine works identically for both database types:

```python
from fd_open_data_mcp.sync.engine import EntitySyncEngine

# Initialize with your database URL
engine = EntitySyncEngine("sqlite:///test.db")

# Sync entities
result = engine.sync_entity_type("stock")
print(f"Inserted: {result['inserted_count']}, Updated: {result['updated_count']}")
```

### MCP Tools

All MCP tools work with both database types:

```python
from fd_open_data_mcp.sync.mcp_tools import (
    list_entity_sources,
    trigger_sync,
    get_sync_history
)

# List configured sources
sources = list_entity_sources()

# Trigger manual sync
result = trigger_sync("stock")

# View sync history
history = get_sync_history(limit=10)
```

## Limitations

### SQLite Limitations

1. **Concurrency**: SQLite has limited write concurrency. Only one writer can access the database at a time.

2. **Performance**: For large datasets (> 1000 entities), SQLite performance degrades significantly.

3. **JSON Querying**: SQLite doesn't support efficient JSON querying like PostgreSQL's JSONB.

4. **File Locks**: File-based locks require file system access and may not work in all environments (e.g., some containerized environments).

### Recommendations

- **Batch Size**: SQLite uses smaller batch sizes (100 vs 500) to reduce lock contention.
- **WAL Mode**: Enable WAL mode for better concurrent read performance:
  ```python
  engine = create_engine("sqlite:///test.db")
  with engine.connect() as conn:
      conn.execute(text("PRAGMA journal_mode=WAL"))
  ```

## Troubleshooting

### Database is locked

**Problem**: SQLite throws "Database is locked" errors.

**Solution**:
1. Reduce batch size in configuration
2. Enable WAL mode
3. Check for long-running transactions
4. Ensure proper lock cleanup in error handlers

### Lock file not found

**Problem**: File lock manager can't create lock files.

**Solution**:
1. Check `SYNC_LOCK_DIR` permissions
2. Ensure directory exists
3. Verify file system is writable

### JSON deserialization errors

**Problem**: JSON data can't be deserialized.

**Solution**:
1. Check data encoding (should be UTF-8)
2. Verify JSON is valid
3. Check for NULL values in metadata columns

## Migration from PostgreSQL to SQLite

If you need to migrate from PostgreSQL to SQLite:

1. Export data from PostgreSQL:
   ```bash
   pg_dump -t entity_sources -t entity_sync_logs -t entity_sync_schedules dbname > export.sql
   ```

2. Create SQLite database and run migrations:
   ```bash
   uv run python scripts/migrate_entity_sync_schema_universal.py "sqlite:///new.db"
   ```

3. Import data (manual conversion required for JSONB to TEXT)

## Migration from SQLite to PostgreSQL

If you need to migrate from SQLite to PostgreSQL:

1. Export data from SQLite:
   ```bash
   sqlite3 database.db .dump > export.sql
   ```

2. Create PostgreSQL database and run migrations:
   ```bash
   uv run python scripts/migrate_entity_sync_schema_universal.py "postgresql://user:pass@localhost/dbname"
   ```

3. Import data (manual conversion required for TEXT to JSONB)

## Testing

Run SQLite compatibility tests:

```bash
uv run pytest tests/test_sqlite_compatibility.py -v
```

## Performance Comparison

| Operation | PostgreSQL | SQLite |
|-----------|-----------|--------|
| Insert 1000 entities | ~2s | ~5s |
| Update 1000 entities | ~3s | ~8s |
| Concurrent syncs | Excellent | Limited |
| JSON queries | Fast | Slow |

## Additional Resources

- [SQLite Documentation](https://www.sqlite.org/docs.html)
- [PostgreSQL Documentation](https://www.postgresql.org/docs/)
- [Entity Sync User Guide](./ENTITY_SYNC_USER_GUIDE.md)

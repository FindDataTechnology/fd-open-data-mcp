## 1. Database Adapter Layer

- [x] 1.1 Create `fd_open_data_mcp/db/adapters/base.py` with abstract DatabaseAdapter class
- [x] 1.2 Implement PostgreSQLAdapter in `fd_open_data_mcp/db/adapters/postgres.py`
- [x] 1.3 Implement SQLiteAdapter in `fd_open_data_mcp/db/adapters/sqlite.py`
- [x] 1.4 Create `fd_open_data_mcp/db/adapters/__init__.py` with adapter factory function
- [x] 1.5 Implement JSON handling methods (jsonb vs text) in both adapters
- [x] 1.6 Implement batch operation methods with database-specific optimizations
- [x] 1.7 Add adapter auto-detection based on database URL

## 2. Lock Abstraction Layer

- [x] 2.1 Create `fd_open_data_mcp/sync/locks/base.py` with abstract LockManager class
- [x] 2.2 Implement AdvisoryLockManager in `fd_open_data_mcp/sync/locks/advisory.py`
- [x] 2.3 Implement FileLockManager in `fd_open_data_mcp/sync/locks/file.py`
- [x] 2.4 Create `fd_open_data_mcp/sync/locks/__init__.py` with lock manager factory
- [x] 2.5 Add lock timeout configuration support
- [x] 2.6 Implement lock context manager protocol
- [x] 2.7 Add lock manager auto-detection based on database type

## 3. Sync Engine Integration

- [x] 3.1 Update `fd_open_data_mcp/sync/engine.py` to use DatabaseAdapter
- [x] 3.2 Update sync engine to use LockManager instead of direct PostgreSQL locks
- [x] 3.3 Modify batch operations to use adapter methods
- [x] 3.4 Update JSON handling to use adapter methods
- [x] 3.5 Add database type detection and adapter initialization
- [x] 3.6 Update error handling for SQLite-specific exceptions

## 4. Migration Scripts

- [x] 4.1 Create SQLite-compatible migration script for entity_sources table
- [x] 4.2 Create SQLite-compatible migration script for entity_sync_logs table
- [x] 4.3 Create SQLite-compatible migration script for entity_sync_schedules table
- [x] 4.4 Update existing migration script to detect database type and use appropriate DDL
- [x] 4.5 Add migration script to handle JSONB to TEXT conversion for SQLite
- [x] 4.6 Test migration scripts on both PostgreSQL and SQLite

## 5. Configuration

- [x] 5.1 Add SYNC_LOCK_TIMEOUT environment variable support
- [x] 5.2 Add SYNC_LOCK_DIR environment variable support
- [x] 5.3 Add database adapter configuration options
- [x] 5.4 Update .env.example with new configuration options
- [x] 5.5 Add configuration validation

## 6. Testing

- [x] 6.1 Create unit tests for PostgreSQLAdapter
- [x] 6.2 Create unit tests for SQLiteAdapter
- [x] 6.3 Create unit tests for AdvisoryLockManager
- [x] 6.4 Create unit tests for FileLockManager
- [x] 6.5 Create integration tests for sync engine with PostgreSQL
- [x] 6.6 Create integration tests for sync engine with SQLite
- [x] 6.7 Test concurrent sync scenarios with file locks
- [x] 6.8 Test lock timeout behavior
- [x] 6.9 Test migration scripts on both database types
- [x] 6.10 Add performance comparison tests (PostgreSQL vs SQLite)

## 7. Documentation

- [x] 7.1 Update README.md with SQLite support information
- [x] 7.2 Create database compatibility guide
- [x] 7.3 Document configuration options for SQLite
- [x] 7.4 Add troubleshooting section for SQLite-specific issues
- [x] 7.5 Update API documentation for adapter interfaces
- [x] 7.6 Create migration guide for existing PostgreSQL users

## 8. Deployment

- [x] 8.1 Update deployment scripts to handle both database types
- [x] 8.2 Add SQLite database initialization to setup scripts
- [x] 8.3 Create Docker configuration for SQLite testing
- [x] 8.4 Update CI/CD pipeline to test both database types
- [x] 8.5 Add database type detection to health check endpoints

## 9. Performance Optimization

- [x] 9.1 Profile SQLite sync performance with large datasets
- [x] 9.2 Optimize batch sizes for SQLite (reduce from 500 to 50-100)
- [x] 9.3 Add SQLite WAL mode configuration
- [x] 9.4 Implement connection pooling for SQLite
- [x] 9.5 Add query optimization for SQLite JSON queries

## 10. Edge Cases

- [x] 10.1 Test sync with empty database
- [x] 10.2 Test sync with corrupted lock files
- [x] 10.3 Test concurrent access to same lock file
- [x] 10.4 Test lock file cleanup on process crash
- [x] 10.5 Test database switching (PostgreSQL to SQLite and vice versa)
- [x] 10.6 Test migration from JSONB to TEXT storage

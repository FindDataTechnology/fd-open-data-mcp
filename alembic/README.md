# Database Migrations

This directory contains Alembic migration scripts for the fd-open-data-mcp ontology database.

## Overview

The migrations manage the PostgreSQL schema for the ontology store, including:
- Entity tables (entities, entity_relationships)
- Catalog tables (sources, functions, columns)
- Semantic layer (concepts, concept_bindings)
- Runtime tables (semantic_observations, fetch_log)

## Running Migrations

### Prerequisites

Set the database URL in your environment:

```bash
export FD_OPEN_DATA_MCP_DATABASE_URL="postgresql://user:pass@host:5433/dbname"
```

Or configure it in `.env` file.

### Apply Migrations

To apply all pending migrations:

```bash
cd fd-open-data-mcp
alembic upgrade head
```

To apply up to a specific revision:

```bash
alembic upgrade <revision_id>
```

### Rollback

To rollback one step:

```bash
alembic downgrade -1
```

To rollback to a specific revision:

```bash
alembic downgrade <revision_id>
```

### Check Status

To see current migration status:

```bash
alembic current
```

To see migration history:

```bash
alembic history
```

## Migration Files

- `001_add_entity_tables.py` - Initial migration: adds entities and entity_relationships tables

## Schema Documentation

### entities

Canonical entity registry storing all entity types (country, stock, industry, etc.).

```sql
CREATE TABLE entities (
    id SERIAL PRIMARY KEY,
    entity_type VARCHAR(32) NOT NULL,  -- country, stock, industry, etc.
    code VARCHAR(128) NOT NULL,        -- canonical identifier (ISO code, ticker, etc.)
    name_en VARCHAR(255),
    name_zh VARCHAR(255),
    metadata_json JSONB,               -- flexible metadata (sector, region, etc.)
    updated_at TIMESTAMP,
    UNIQUE (entity_type, code)
);
```

### entity_relationships

Entity-to-entity relationships with temporal validity.

```sql
CREATE TABLE entity_relationships (
    id SERIAL PRIMARY KEY,
    source_id INTEGER REFERENCES entities(id) ON DELETE CASCADE,
    relation_type VARCHAR(64) NOT NULL,  -- belongs_to, has_sector, etc.
    target_id INTEGER REFERENCES entities(id) ON DELETE CASCADE,
    valid_from TIMESTAMP,
    valid_to TIMESTAMP,
    metadata_json JSONB,
    created_at TIMESTAMP,
    UNIQUE (source_id, relation_type, target_id, valid_from)
);
```

## Creating New Migrations

To create a new migration after schema changes:

```bash
# Auto-generate from model changes
alembic revision --autogenerate -m "description"

# Or create empty migration
alembic revision -m "description"
```

Then edit the generated file in `alembic/versions/`.

## Testing Migrations

Test migrations against a local database before applying to production:

```bash
# Create test database
createdb fd_open_data_mcp_test

# Run migrations
FD_OPEN_DATA_MCP_DATABASE_URL="postgresql://localhost/fd_open_data_mcp_test" \
  alembic upgrade head

# Verify schema
psql fd_open_data_mcp_test -c "\dt"

# Cleanup
dropdb fd_open_data_mcp_test
```

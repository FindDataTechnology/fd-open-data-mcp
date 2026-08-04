# database-adapter Specification

## Purpose
TBD - created by archiving change sqlite-compatibility-for-sync. Update Purpose after archive.
## Requirements
### Requirement: Database adapter abstraction layer

The system SHALL provide a database adapter abstraction layer that hides database-specific SQL syntax and data types behind a unified interface. The adapter SHALL be selected automatically based on the database URL scheme (postgresql:// or sqlite://).

#### Scenario: PostgreSQL adapter selected automatically

- **WHEN** FD_OPEN_DATA_MCP_DATABASE_URL starts with "postgresql://"
- **THEN** the system SHALL use PostgreSQLAdapter
- **AND** all database operations SHALL use PostgreSQL-specific syntax (JSONB, SERIAL, etc.)

#### Scenario: SQLite adapter selected automatically

- **WHEN** FD_OPEN_DATA_MCP_DATABASE_URL starts with "sqlite://"
- **THEN** the system SHALL use SQLiteAdapter
- **AND** all database operations SHALL use SQLite-compatible syntax (TEXT for JSON, AUTOINCREMENT, etc.)

### Requirement: Adapter-based JSON storage

The system SHALL store JSON metadata using database-appropriate types. PostgreSQL adapters SHALL use JSONB type for efficient querying. SQLite adapters SHALL use TEXT type with JSON string serialization.

#### Scenario: PostgreSQL JSONB storage

- **WHEN** using PostgreSQLAdapter
- **THEN** metadata_json columns SHALL be created as JSONB type
- **AND** JSON operations SHALL use PostgreSQL JSON functions (->>, jsonb_build_object)

#### Scenario: SQLite TEXT storage

- **WHEN** using SQLiteAdapter
- **THEN** metadata_json columns SHALL be created as TEXT type
- **AND** JSON data SHALL be serialized to string using json.dumps()
- **AND** JSON operations SHALL use Python json.loads() for deserialization

### Requirement: Adapter-based schema creation

The system SHALL provide adapter-specific schema creation methods that generate correct SQL for each database type. Migration scripts SHALL use the adapter to create tables with appropriate column types and constraints.

#### Scenario: PostgreSQL schema creation

- **WHEN** creating sync tables with PostgreSQLAdapter
- **THEN** tables SHALL use SERIAL for auto-increment IDs
- **AND** metadata columns SHALL use JSONB type
- **AND** timestamp columns SHALL use TIMESTAMP WITH TIME ZONE

#### Scenario: SQLite schema creation

- **WHEN** creating sync tables with SQLiteAdapter
- **THEN** tables SHALL use INTEGER PRIMARY KEY AUTOINCREMENT for IDs
- **AND** metadata columns SHALL use TEXT type
- **AND** timestamp columns SHALL use TEXT (ISO 8601 format)

### Requirement: Adapter-based batch operations

The system SHALL provide adapter-specific batch insert and update methods that handle database-specific syntax differences. PostgreSQL adapters SHALL use PostgreSQL INSERT ... ON CONFLICT syntax. SQLite adapters SHALL use SQLite INSERT OR REPLACE syntax.

#### Scenario: PostgreSQL batch insert

- **WHEN** inserting entities with PostgreSQLAdapter
- **THEN** the system SHALL use INSERT ... ON CONFLICT (entity_type, code) DO UPDATE
- **AND** batch size SHALL be 500 rows per transaction

#### Scenario: SQLite batch insert

- **WHEN** inserting entities with SQLiteAdapter
- **THEN** the system SHALL use INSERT OR REPLACE
- **AND** batch size SHALL be 100 rows per transaction (reduced for SQLite concurrency limits)


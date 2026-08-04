## ADDED Requirements

### Requirement: Lock abstraction layer
The system SHALL provide a lock abstraction layer that automatically selects the appropriate locking mechanism based on the database type.

#### Scenario: PostgreSQL environment uses advisory locks
- **WHEN** the system runs with PostgreSQL database
- **THEN** it SHALL use `pg_advisory_xact_lock()` for synchronization
- **AND** the lock mechanism SHALL be transparent to the caller

#### Scenario: SQLite environment uses file locks
- **WHEN** the system runs with SQLite database
- **THEN** it SHALL use file-based locks for synchronization
- **AND** lock files SHALL be stored in a dedicated directory (e.g., `/tmp/entity_sync_lock/`)
- **AND** each entity_type SHALL have its own lock file

#### Scenario: Lock acquisition and release
- **WHEN** a sync operation begins
- **THEN** it SHALL acquire the appropriate lock for the entity_type
- **AND** when the operation completes (success or failure)
- **THEN** the lock SHALL be automatically released

### Requirement: File-based lock implementation
The system SHALL implement a file-based lock mechanism for SQLite environments that provides mutual exclusion.

#### Scenario: File lock creation
- **WHEN** a sync operation requests a lock for entity_type='stock'
- **THEN** the system SHALL create or open a lock file (e.g., `/tmp/entity_sync_lock/stock.lock`)
- **AND** it SHALL acquire an exclusive lock on the file using `fcntl.flock()`

#### Scenario: File lock contention
- **WHEN** multiple processes attempt to sync the same entity_type simultaneously
- **THEN** only one process SHALL acquire the lock
- **AND** other processes SHALL either wait or fail gracefully based on configuration

#### Scenario: File lock cleanup
- **WHEN** the sync operation completes
- **THEN** the file lock SHALL be released
- **AND** the lock file MAY be deleted or left for reuse

### Requirement: Lock configuration
The system SHALL allow configuration of lock behavior through environment variables or configuration files.

#### Scenario: Configure lock timeout
- **WHEN** the environment variable `SYNC_LOCK_TIMEOUT` is set to 30
- **THEN** lock acquisition SHALL timeout after 30 seconds
- **AND** a timeout error SHALL be raised if the lock cannot be acquired

#### Scenario: Configure lock directory
- **WHEN** the environment variable `SYNC_LOCK_DIR` is set to `/var/run/sync-locks`
- **THEN** file-based locks SHALL be stored in that directory
- **AND** the directory SHALL be created if it does not exist

#### Scenario: Default lock configuration
- **WHEN** no lock configuration is provided
- **THEN** the system SHALL use sensible defaults (timeout=60s, directory=/tmp/entity_sync_lock)

### Requirement: Lock abstraction interface
The system SHALL provide a unified interface for lock operations that works across all database types.

#### Scenario: Unified lock API
- **WHEN** code calls `LockManager.acquire(entity_type='stock')`
- **THEN** it SHALL work identically whether the backend is PostgreSQL or SQLite
- **AND** the caller SHALL NOT need to know which locking mechanism is used

#### Scenario: Lock context manager
- **WHEN** code uses the lock as a context manager
- **THEN** the lock SHALL be automatically acquired on entry
- **AND** released on exit (even if an exception occurs)

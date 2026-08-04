"""PostgreSQL database adapter implementation.

This module provides PostgreSQL-specific implementations for JSON handling,
schema creation, and batch operations.
"""
import json
from datetime import datetime
from typing import Any, Dict, List, Optional
from sqlalchemy import text
from sqlalchemy.orm import Session

from .base import DatabaseAdapter


class PostgreSQLAdapter(DatabaseAdapter):
    """PostgreSQL-specific database adapter.

    Uses PostgreSQL-specific features:
    - JSONB type for efficient JSON storage and querying
    - SERIAL for auto-increment primary keys
    - pg_advisory_xact_lock for synchronization
    - INSERT ... ON CONFLICT for upsert operations
    """

    def __init__(self, database_url: str):
        """Initialize PostgreSQL adapter.

        Args:
            database_url: PostgreSQL connection string
        """
        super().__init__(database_url)
        self.batch_size = 500  # PostgreSQL can handle larger batches

    def get_json_type(self) -> str:
        """Get the appropriate JSON column type for PostgreSQL."""
        return "JSONB"

    def get_autoincrement_type(self) -> str:
        """Get the auto-increment primary key type for PostgreSQL."""
        return "SERIAL PRIMARY KEY"

    def serialize_json(self, data: Any) -> str:
        """Serialize Python object to JSON string.

        Args:
            data: Python object to serialize

        Returns:
            JSON string
        """
        if data is None:
            return None
        return json.dumps(data, ensure_ascii=False)

    def deserialize_json(self, json_str: str) -> Any:
        """Deserialize JSON string to Python object.

        Args:
            json_str: JSON string to deserialize

        Returns:
            Python object
        """
        if json_str is None:
            return None
        if isinstance(json_str, dict):
            # Already a dict (PostgreSQL JSONB returns dict directly)
            return json_str
        return json.loads(json_str)

    def get_batch_size(self) -> int:
        """Get the recommended batch size for bulk operations."""
        return self.batch_size

    def get_insert_conflict_clause(self, table: str, conflict_columns: List[str]) -> str:
        """Get the SQL clause for handling insert conflicts (upsert)."""
        conflict_cols_sql = ", ".join(conflict_columns)
        return f"ON CONFLICT ({conflict_cols_sql}) DO UPDATE SET"

    def get_timestamp_type(self) -> str:
        """Get the appropriate timestamp column type for PostgreSQL."""
        return "TIMESTAMP WITH TIME ZONE"

    def format_timestamp(self, timestamp: Any) -> str:
        """Format a timestamp for storage in PostgreSQL."""
        if isinstance(timestamp, datetime):
            return timestamp.isoformat()
        return str(timestamp)

    def parse_timestamp(self, timestamp_str: str) -> Any:
        """Parse a timestamp string from PostgreSQL."""
        if timestamp_str is None:
            return None
        return datetime.fromisoformat(timestamp_str.replace('Z', '+00:00'))

    def create_table_sql(self, table_name: str, columns: Dict[str, str]) -> str:
        """Generate CREATE TABLE SQL for PostgreSQL.

        Args:
            table_name: Name of the table
            columns: Dictionary of column_name -> column_type

        Returns:
            CREATE TABLE SQL statement
        """
        # PostgreSQL uses SERIAL for auto-increment
        col_defs = []
        for col_name, col_type in columns.items():
            if col_name == "id":
                col_defs.append(f"{col_name} SERIAL PRIMARY KEY")
            elif col_type == "JSON":
                # PostgreSQL uses JSONB for JSON storage
                col_defs.append(f"{col_name} JSONB")
            else:
                col_defs.append(f"{col_name} {col_type}")

        cols_sql = ",\n    ".join(col_defs)
        return f"CREATE TABLE IF NOT EXISTS {table_name} (\n    {cols_sql}\n)"

    def batch_insert_sql(self, table_name: str, columns: List[str]) -> str:
        """Generate batch INSERT SQL for PostgreSQL.

        Uses INSERT ... ON CONFLICT for upsert operations.

        Args:
            table_name: Name of the table
            columns: List of column names

        Returns:
            INSERT SQL template with placeholders
        """
        cols = ", ".join(columns)
        placeholders = ", ".join([f":{col}" for col in columns])

        # For entity_sources table, use entity_type as conflict target
        if table_name == "entity_sources":
            conflict_cols = ["entity_type"]
        elif table_name == "entity_sync_logs":
            conflict_cols = ["id"]  # Logs don't upsert, just insert
        elif table_name == "entity_sync_schedules":
            conflict_cols = ["entity_type"]
        else:
            conflict_cols = ["id"]

        conflict_cols_sql = ", ".join(conflict_cols)

        # Build UPDATE SET clause for non-conflict columns
        update_cols = [col for col in columns if col not in conflict_cols]
        update_set = ", ".join([f"{col} = EXCLUDED.{col}" for col in update_cols])

        if update_set:
            return f"""
                INSERT INTO {table_name} ({cols})
                VALUES ({placeholders})
                ON CONFLICT ({conflict_cols_sql}) DO UPDATE SET {update_set}
            """
        else:
            return f"""
                INSERT INTO {table_name} ({cols})
                VALUES ({placeholders})
                ON CONFLICT ({conflict_cols_sql}) DO NOTHING
            """

    def batch_update_sql(self, table_name: str, columns: List[str], where_cols: List[str]) -> str:
        """Generate batch UPDATE SQL for PostgreSQL.

        Args:
            table_name: Name of the table
            columns: List of columns to update
            where_cols: List of columns for WHERE clause

        Returns:
            UPDATE SQL template with placeholders
        """
        set_clause = ", ".join([f"{col} = :{col}" for col in columns])
        where_clause = " AND ".join([f"{col} = :where_{col}" for col in where_cols])

        return f"""
            UPDATE {table_name}
            SET {set_clause}
            WHERE {where_clause}
        """

    def acquire_lock(self, session: Session, lock_name: str) -> bool:
        """Acquire PostgreSQL advisory lock.

        Uses pg_advisory_xact_lock for transaction-level locking.

        Args:
            session: SQLAlchemy session
            lock_name: Name of the lock (e.g., "sync:stock")

        Returns:
            True if lock acquired successfully
        """
        # Convert lock name to integer hash
        lock_hash = hash(lock_name) & 0x7FFFFFFF  # Ensure positive integer

        try:
            session.execute(text(f"SELECT pg_advisory_xact_lock(:lock_hash)"), {"lock_hash": lock_hash})
            return True
        except Exception as e:
            self.logger.error(f"Failed to acquire PostgreSQL lock: {e}")
            return False

    def release_lock(self, session: Session, lock_name: str) -> bool:
        """Release PostgreSQL advisory lock.

        Note: Transaction-level locks are automatically released on commit/rollback.
        This method is provided for explicit release if needed.

        Args:
            session: SQLAlchemy session
            lock_name: Name of the lock

        Returns:
            True if lock released successfully
        """
        # Transaction-level locks are automatically released
        # This is a no-op for PostgreSQL
        return True

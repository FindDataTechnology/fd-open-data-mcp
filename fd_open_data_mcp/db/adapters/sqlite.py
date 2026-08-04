"""SQLite database adapter implementation.

This module provides SQLite-specific implementations for JSON handling,
schema creation, and batch operations.
"""
import json
import os
from datetime import datetime
from typing import Any, Dict, List, Optional
from sqlalchemy import text
from sqlalchemy.orm import Session

from .base import DatabaseAdapter


class SQLiteAdapter(DatabaseAdapter):
    """SQLite-specific database adapter.

    Uses SQLite-compatible features:
    - TEXT type for JSON storage (serialized as strings)
    - INTEGER PRIMARY KEY AUTOINCREMENT for auto-increment
    - File-based locks for synchronization
    - INSERT OR REPLACE for upsert operations
    """

    def __init__(self, database_url: str):
        """Initialize SQLite adapter.

        Args:
            database_url: SQLite connection string
        """
        super().__init__(database_url)
        self.batch_size = 100  # SQLite needs smaller batches due to concurrency limits

        # Lock configuration
        self.lock_dir = os.environ.get("SYNC_LOCK_DIR", "/tmp/entity_sync_lock")
        self.lock_timeout = int(os.environ.get("SYNC_LOCK_TIMEOUT", "60"))
        self._lock_files = {}  # Track open lock files for cleanup

    def get_json_type(self) -> str:
        """Get the appropriate JSON column type for SQLite."""
        return "TEXT"

    def get_autoincrement_type(self) -> str:
        """Get the auto-increment primary key type for SQLite."""
        return "INTEGER PRIMARY KEY AUTOINCREMENT"

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
            # Already a dict (shouldn't happen with SQLite, but handle it)
            return json_str
        return json.loads(json_str)

    def get_batch_size(self) -> int:
        """Get the recommended batch size for bulk operations."""
        return self.batch_size

    def get_insert_conflict_clause(self, table: str, conflict_columns: List[str]) -> str:
        """Get the SQL clause for handling insert conflicts (upsert)."""
        # SQLite uses INSERT OR REPLACE
        return "INSERT OR REPLACE INTO"

    def get_timestamp_type(self) -> str:
        """Get the appropriate timestamp column type for SQLite."""
        return "TEXT"  # SQLite stores timestamps as TEXT (ISO 8601)

    def format_timestamp(self, timestamp: Any) -> str:
        """Format a timestamp for storage in SQLite."""
        if isinstance(timestamp, datetime):
            return timestamp.isoformat()
        return str(timestamp)

    def parse_timestamp(self, timestamp_str: str) -> Any:
        """Parse a timestamp string from SQLite."""
        if timestamp_str is None:
            return None
        return datetime.fromisoformat(timestamp_str.replace('Z', '+00:00'))

    def create_table_sql(self, table_name: str, columns: Dict[str, str]) -> str:
        """Generate CREATE TABLE SQL for SQLite.

        Args:
            table_name: Name of the table
            columns: Dictionary of column_name -> column_type

        Returns:
            CREATE TABLE SQL statement
        """
        # SQLite uses INTEGER PRIMARY KEY AUTOINCREMENT
        col_defs = []
        for col_name, col_type in columns.items():
            if col_name == "id":
                col_defs.append(f"{col_name} INTEGER PRIMARY KEY AUTOINCREMENT")
            elif col_type == "JSON":
                # SQLite uses TEXT for JSON storage
                col_defs.append(f"{col_name} TEXT")
            elif col_type == "TIMESTAMP":
                # SQLite stores timestamps as TEXT (ISO 8601)
                col_defs.append(f"{col_name} TEXT")
            else:
                col_defs.append(f"{col_name} {col_type}")

        cols_sql = ",\n    ".join(col_defs)
        return f"CREATE TABLE IF NOT EXISTS {table_name} (\n    {cols_sql}\n)"

    def batch_insert_sql(self, table_name: str, columns: List[str]) -> str:
        """Generate batch INSERT SQL for SQLite.

        Uses INSERT OR REPLACE for upsert operations.

        Args:
            table_name: Name of the table
            columns: List of column names

        Returns:
            INSERT SQL template with placeholders
        """
        cols = ", ".join(columns)
        placeholders = ", ".join([f":{col}" for col in columns])

        # SQLite uses INSERT OR REPLACE for upsert
        return f"""
            INSERT OR REPLACE INTO {table_name} ({cols})
            VALUES ({placeholders})
        """

    def batch_update_sql(self, table_name: str, columns: List[str], where_cols: List[str]) -> str:
        """Generate batch UPDATE SQL for SQLite.

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
        """Acquire file-based lock for SQLite.

        Uses fcntl.flock() for file locking on Unix systems.

        Args:
            session: SQLAlchemy session (not used for SQLite)
            lock_name: Name of the lock (e.g., "sync:stock")

        Returns:
            True if lock acquired successfully
        """
        import fcntl
        from pathlib import Path

        # Create lock directory if it doesn't exist
        lock_path = Path(self.lock_dir)
        lock_path.mkdir(parents=True, exist_ok=True)

        # Create lock file path
        lock_file = lock_path / f"{lock_name.replace(':', '_')}.lock"

        try:
            # Open lock file
            f = open(lock_file, 'w')
            self._lock_files[lock_name] = f

            # Try to acquire exclusive lock (non-blocking)
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.logger.debug(f"Acquired file lock: {lock_file}")
            return True

        except (IOError, OSError) as e:
            self.logger.error(f"Failed to acquire file lock {lock_file}: {e}")
            # Clean up if we opened the file
            if lock_name in self._lock_files:
                try:
                    self._lock_files[lock_name].close()
                    del self._lock_files[lock_name]
                except Exception:
                    pass
            return False

    def release_lock(self, session: Session, lock_name: str) -> bool:
        """Release file-based lock for SQLite.

        Args:
            session: SQLAlchemy session (not used for SQLite)
            lock_name: Name of the lock

        Returns:
            True if lock released successfully
        """
        import fcntl

        if lock_name not in self._lock_files:
            self.logger.warning(f"No lock file found for {lock_name}")
            return True

        try:
            f = self._lock_files[lock_name]
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            f.close()
            del self._lock_files[lock_name]
            self.logger.debug(f"Released file lock: {lock_name}")
            return True

        except Exception as e:
            self.logger.error(f"Failed to release file lock {lock_name}: {e}")
            return False

    def __del__(self):
        """Cleanup: release all open locks on destruction."""
        for lock_name in list(self._lock_files.keys()):
            try:
                self.release_lock(None, lock_name)
            except Exception:
                pass

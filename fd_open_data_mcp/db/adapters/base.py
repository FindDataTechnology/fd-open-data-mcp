"""
Database adapter abstraction layer.

Provides a unified interface for database operations across different database types
(PostgreSQL, SQLite). Each adapter handles database-specific SQL syntax and data types.
"""
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional
import json


class DatabaseAdapter(ABC):
    """Abstract base class for database adapters."""

    def __init__(self, database_url: str):
        """
        Initialize the database adapter.

        Args:
            database_url: Database connection URL
        """
        self.database_url = database_url

    @abstractmethod
    def get_json_type(self) -> str:
        """
        Get the appropriate JSON column type for this database.

        Returns:
            SQL type name (e.g., 'JSONB' for PostgreSQL, 'TEXT' for SQLite)
        """
        pass

    @abstractmethod
    def get_autoincrement_type(self) -> str:
        """
        Get the auto-increment primary key type for this database.

        Returns:
            SQL type definition (e.g., 'SERIAL PRIMARY KEY' for PostgreSQL,
            'INTEGER PRIMARY KEY AUTOINCREMENT' for SQLite)
        """
        pass

    @abstractmethod
    def serialize_json(self, data: Any) -> str:
        """
        Serialize Python data to JSON string for storage.

        Args:
            data: Python object to serialize (dict, list, etc.)

        Returns:
            JSON string
        """
        pass

    @abstractmethod
    def deserialize_json(self, json_str: str) -> Any:
        """
        Deserialize JSON string from database to Python object.

        Args:
            json_str: JSON string from database

        Returns:
            Python object (dict, list, etc.)
        """
        pass

    @abstractmethod
    def get_batch_size(self) -> int:
        """
        Get the recommended batch size for bulk operations.

        Returns:
            Number of rows per batch
        """
        pass

    @abstractmethod
    def get_insert_conflict_clause(self, table: str, conflict_columns: List[str]) -> str:
        """
        Get the SQL clause for handling insert conflicts (upsert).

        Args:
            table: Table name
            conflict_columns: Columns that define uniqueness

        Returns:
            SQL clause string (e.g., 'ON CONFLICT ... DO UPDATE')
        """
        pass

    @abstractmethod
    def get_timestamp_type(self) -> str:
        """
        Get the appropriate timestamp column type for this database.

        Returns:
            SQL type name (e.g., 'TIMESTAMP WITH TIME ZONE' for PostgreSQL,
            'TEXT' for SQLite)
        """
        pass

    @abstractmethod
    def format_timestamp(self, timestamp: Any) -> str:
        """
        Format a timestamp for storage in this database.

        Args:
            timestamp: Python datetime object or ISO string

        Returns:
            Formatted timestamp string
        """
        pass

    @abstractmethod
    def parse_timestamp(self, timestamp_str: str) -> Any:
        """
        Parse a timestamp string from this database.

        Args:
            timestamp_str: Timestamp string from database

        Returns:
            Python datetime object
        """
        pass

    def get_schema_sql(self, table_name: str, columns: Dict[str, str]) -> str:
        """
        Generate CREATE TABLE SQL for this database.

        Args:
            table_name: Name of the table
            columns: Dictionary of column_name -> column_type

        Returns:
            CREATE TABLE SQL statement
        """
        column_defs = [f"{name} {type_def}" for name, type_def in columns.items()]
        columns_sql = ", ".join(column_defs)
        return f"CREATE TABLE IF NOT EXISTS {table_name} ({columns_sql})"

    def get_database_type(self) -> str:
        """
        Get the database type identifier.

        Returns:
            Database type string ('postgresql' or 'sqlite')
        """
        if self.database_url.startswith("postgresql"):
            return "postgresql"
        elif self.database_url.startswith("sqlite"):
            return "sqlite"
        else:
            raise ValueError(f"Unsupported database type: {self.database_url}")

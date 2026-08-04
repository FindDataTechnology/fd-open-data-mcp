"""Database adapter factory and utilities.

This module provides automatic database adapter selection based on the database URL,
and exports all adapter classes for convenience.
"""
import os
from typing import Optional
from .base import DatabaseAdapter
from .postgres import PostgreSQLAdapter
from .sqlite import SQLiteAdapter


__all__ = [
    "DatabaseAdapter",
    "PostgreSQLAdapter",
    "SQLiteAdapter",
    "get_adapter",
    "get_adapter_from_env",
]


def get_adapter(database_url: Optional[str] = None) -> DatabaseAdapter:
    """Get the appropriate database adapter based on the database URL.

    Automatically detects the database type from the URL scheme and returns
    the corresponding adapter instance.

    Args:
        database_url: Database connection string. If None, reads from
                     FD_OPEN_DATA_MCP_DATABASE_URL environment variable.

    Returns:
        DatabaseAdapter instance (PostgreSQLAdapter or SQLiteAdapter)

    Raises:
        ValueError: If the database URL scheme is not supported

    Examples:
        >>> adapter = get_adapter("postgresql://user:pass@localhost/db")
        >>> isinstance(adapter, PostgreSQLAdapter)
        True

        >>> adapter = get_adapter("sqlite:///path/to/db.sqlite")
        >>> isinstance(adapter, SQLiteAdapter)
        True
    """
    if database_url is None:
        database_url = os.environ.get(
            "FD_OPEN_DATA_MCP_DATABASE_URL",
            "sqlite:///metadata/daas.db"
        )

    if database_url.startswith("postgresql://") or database_url.startswith("postgres://"):
        return PostgreSQLAdapter(database_url)
    elif database_url.startswith("sqlite://"):
        return SQLiteAdapter(database_url)
    else:
        raise ValueError(
            f"Unsupported database URL scheme: {database_url}. "
            "Supported schemes: postgresql://, sqlite://"
        )


def get_adapter_from_env() -> DatabaseAdapter:
    """Get database adapter from environment variable.

    Convenience function that reads FD_OPEN_DATA_MCP_DATABASE_URL and returns
    the appropriate adapter.

    Returns:
        DatabaseAdapter instance

    Raises:
        ValueError: If the environment variable is not set or contains unsupported scheme
    """
    return get_adapter()

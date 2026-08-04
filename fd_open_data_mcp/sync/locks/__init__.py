"""Lock manager factory and utilities.

This module provides automatic lock manager selection based on the database type,
and exports all lock manager classes for convenience.
"""
import os
from typing import Optional
from .base import LockManager
from .advisory import AdvisoryLockManager
from .file import FileLockManager


__all__ = [
    "LockManager",
    "AdvisoryLockManager",
    "FileLockManager",
    "get_lock_manager",
    "get_lock_manager_from_env",
]


def get_lock_manager(
    database_url: str,
    timeout: int = 60,
    lock_dir: Optional[str] = None,
) -> LockManager:
    """Get the appropriate lock manager based on the database URL.

    Automatically detects the database type from the URL scheme and returns
    the corresponding lock manager instance.

    Args:
        database_url: Database connection string
        timeout: Lock acquisition timeout in seconds (default: 60)
        lock_dir: Directory for file-based locks (overrides SYNC_LOCK_DIR env var)

    Returns:
        LockManager instance (AdvisoryLockManager or FileLockManager)

    Raises:
        ValueError: If the database URL scheme is not supported
    """
    # Determine lock directory from parameter or environment
    effective_lock_dir = lock_dir or os.environ.get("SYNC_LOCK_DIR", "/tmp/entity_sync_lock")

    if database_url.startswith("postgresql://") or database_url.startswith("postgres://"):
        return AdvisoryLockManager(timeout=timeout)
    elif database_url.startswith("sqlite://"):
        return FileLockManager(lock_dir=effective_lock_dir, timeout=timeout)
    else:
        raise ValueError(
            f"Unsupported database URL scheme: {database_url}. "
            "Supported schemes: postgresql://, sqlite://"
        )


def get_lock_manager_from_env(
    timeout: Optional[int] = None,
    lock_dir: Optional[str] = None,
) -> LockManager:
    """Get lock manager from environment variables.

    Convenience function that reads FD_OPEN_DATA_MCP_DATABASE_URL and other
    relevant environment variables to return the appropriate lock manager.

    Args:
        timeout: Lock acquisition timeout (overrides SYNC_LOCK_TIMEOUT env var)
        lock_dir: Directory for file-based locks (overrides SYNC_LOCK_DIR env var)

    Returns:
        LockManager instance

    Raises:
        ValueError: If the environment variable is not set or contains unsupported scheme
    """
    database_url = os.environ.get(
        "FD_OPEN_DATA_MCP_DATABASE_URL",
        "sqlite:///metadata/daas.db"
    )

    # Get timeout from parameter or environment
    effective_timeout = timeout if timeout is not None else int(os.environ.get("SYNC_LOCK_TIMEOUT", "60"))

    return get_lock_manager(database_url, timeout=effective_timeout, lock_dir=lock_dir)

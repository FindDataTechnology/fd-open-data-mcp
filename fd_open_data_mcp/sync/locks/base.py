"""Abstract base class for lock managers.

This module defines the interface for database-specific lock implementations.
Lock managers provide mutual exclusion for sync operations to prevent
concurrent conflicts when multiple processes attempt to sync the same entity type.
"""
from abc import ABC, abstractmethod
from typing import Optional
from sqlalchemy.orm import Session


class LockManager(ABC):
    """Abstract base class for lock managers.

    Lock managers provide a unified interface for acquiring and releasing
    locks, regardless of the underlying database type. The specific implementation
    (PostgreSQL advisory locks or file-based locks) is hidden behind this interface.

    Attributes:
        lock_timeout: Timeout in seconds for lock acquisition (default: 60)
    """

    def __init__(self, lock_timeout: int = 60):
        """Initialize lock manager.

        Args:
            lock_timeout: Timeout in seconds for lock acquisition
        """
        self.lock_timeout = lock_timeout

    @abstractmethod
    def acquire(self, session: Session, lock_name: str) -> bool:
        """Acquire a lock for the given lock name.

        Args:
            session: SQLAlchemy database session
            lock_name: Name of the lock (e.g., "sync:stock")

        Returns:
            True if lock acquired successfully, False otherwise
        """
        pass

    @abstractmethod
    def release(self, session: Session, lock_name: str) -> bool:
        """Release a lock for the given lock name.

        Args:
            session: SQLAlchemy database session
            lock_name: Name of the lock

        Returns:
            True if lock released successfully, False otherwise
        """
        pass

    def __enter__(self):
        """Enter context manager - acquire lock."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Exit context manager - release lock."""
        return False

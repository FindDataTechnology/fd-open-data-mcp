"""PostgreSQL advisory lock manager implementation.

This module provides PostgreSQL-specific lock management using advisory locks.
Advisory locks are application-level locks that don't lock tables or rows,
but provide mutual exclusion for application-defined resources.
"""
import logging
from typing import Optional
from sqlalchemy import text
from sqlalchemy.orm import Session

from .base import LockManager


logger = logging.getLogger(__name__)


class AdvisoryLockManager(LockManager):
    """PostgreSQL advisory lock manager.

    Uses PostgreSQL's pg_advisory_xact_lock() function for transaction-level
    advisory locks. These locks are automatically released when the transaction
    ends (commit or rollback).

    Advantages:
    - No file system dependencies
    - Automatic cleanup on transaction end
    - Fast and efficient (memory-based)
    - Supports lock timeouts
    """

    def __init__(self, timeout: int = 60):
        """Initialize advisory lock manager.

        Args:
            timeout: Lock acquisition timeout in seconds (default: 60)
        """
        super().__init__(timeout)
        self._lock_hashes = {}  # Track acquired lock hashes for cleanup

    def _name_to_hash(self, lock_name: str) -> int:
        """Convert lock name to integer hash for PostgreSQL advisory lock.

        PostgreSQL advisory locks use two 32-bit integers or one 64-bit integer.
        We use a single 64-bit integer derived from the lock name hash.

        Args:
            lock_name: Name of the lock (e.g., "sync:stock")

        Returns:
            64-bit integer hash
        """
        # Use Python's built-in hash and ensure it's positive
        hash_value = hash(lock_name) & 0x7FFFFFFFFFFFFFFF
        return hash_value

    def acquire(self, session: Session, lock_name: str) -> bool:
        """Acquire PostgreSQL advisory lock.

        Uses pg_advisory_xact_lock() which automatically releases the lock
        when the transaction ends.

        Args:
            session: SQLAlchemy session
            lock_name: Name of the lock (e.g., "sync:stock")

        Returns:
            True if lock acquired successfully, False otherwise
        """
        lock_hash = self._name_to_hash(lock_name)

        try:
            # Set lock timeout
            if self.timeout > 0:
                session.execute(text(f"SET lock_timeout = '{self.timeout}s'"))

            # Try to acquire advisory lock
            # pg_advisory_xact_lock() blocks until lock is available or timeout
            session.execute(text("SELECT pg_advisory_xact_lock(:lock_hash)"), {"lock_hash": lock_hash})

            self._lock_hashes[lock_name] = lock_hash
            logger.debug(f"Acquired PostgreSQL advisory lock: {lock_name} (hash: {lock_hash})")
            return True

        except Exception as e:
            logger.error(f"Failed to acquire PostgreSQL advisory lock {lock_name}: {e}")
            return False

    def release(self, session: Session, lock_name: str) -> bool:
        """Release PostgreSQL advisory lock.

        Note: Transaction-level advisory locks are automatically released when
        the transaction ends. This method is provided for explicit release if needed,
        but typically not necessary.

        Args:
            session: SQLAlchemy session
            lock_name: Name of the lock

        Returns:
            True if lock released successfully, False otherwise
        """
        if lock_name not in self._lock_hashes:
            logger.warning(f"Lock {lock_name} not found in tracked locks")
            return True

        try:
            # Explicitly release the lock (optional, as it's auto-released on tx end)
            lock_hash = self._lock_hashes[lock_name]
            session.execute(text("SELECT pg_advisory_unlock(:lock_hash)"), {"lock_hash": lock_hash})

            del self._lock_hashes[lock_name]
            logger.debug(f"Released PostgreSQL advisory lock: {lock_name}")
            return True

        except Exception as e:
            logger.error(f"Failed to release PostgreSQL advisory lock {lock_name}: {e}")
            return False

    def is_locked(self, session: Session, lock_name: str) -> bool:
        """Check if a lock is currently held.

        Note: This checks if the current session holds the lock, not if any
        session holds it. PostgreSQL advisory locks can be held by multiple
        sessions simultaneously (they're not mutually exclusive across sessions
        unless explicitly managed).

        Args:
            session: SQLAlchemy session
            lock_name: Name of the lock

        Returns:
            True if the current session holds the lock
        """
        return lock_name in self._lock_hashes

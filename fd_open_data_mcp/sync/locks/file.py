"""File-based lock manager implementation for SQLite.

This module provides file-based lock management using fcntl.flock() on Unix systems.
File locks provide mutual exclusion for SQLite environments where PostgreSQL advisory
locks are not available.
"""
import fcntl
import logging
import os
from pathlib import Path
from typing import Optional
from sqlalchemy.orm import Session

from .base import LockManager


logger = logging.getLogger(__name__)


class FileLockManager(LockManager):
    """File-based lock manager for SQLite environments.

    Uses fcntl.flock() for file locking on Unix systems. Each lock is represented
    as a separate file in a dedicated lock directory.

    Advantages:
    - Works with SQLite (no database-specific features needed)
    - Cross-process mutual exclusion
    - Automatic cleanup on process exit (OS releases file locks)

    Limitations:
    - Unix-only (fcntl not available on Windows)
    - Requires file system access
    - Slightly slower than advisory locks
    """

    def __init__(self, lock_dir: str = "/tmp/entity_sync_lock", timeout: int = 60):
        """Initialize file lock manager.

        Args:
            lock_dir: Directory to store lock files (default: /tmp/entity_sync_lock)
            timeout: Lock acquisition timeout in seconds (default: 60)
        """
        super().__init__(timeout)
        self.lock_dir = Path(lock_dir)
        self._lock_files = {}  # Track open lock files for cleanup

        # Create lock directory if it doesn't exist
        self.lock_dir.mkdir(parents=True, exist_ok=True)

    def _get_lock_file_path(self, lock_name: str) -> Path:
        """Get the file path for a lock name.

        Args:
            lock_name: Name of the lock (e.g., "sync:stock")

        Returns:
            Path to the lock file
        """
        # Replace colons with underscores for safe file names
        safe_name = lock_name.replace(":", "_").replace("/", "_")
        return self.lock_dir / f"{safe_name}.lock"

    def acquire(self, session: Session, lock_name: str) -> bool:
        """Acquire file-based lock.

        Uses fcntl.flock() to acquire an exclusive lock on a file. The lock is
        automatically released when the file is closed or the process exits.

        Args:
            session: SQLAlchemy session (not used for file locks)
            lock_name: Name of the lock (e.g., "sync:stock")

        Returns:
            True if lock acquired successfully, False otherwise
        """
        lock_file_path = self._get_lock_file_path(lock_name)

        try:
            # Open lock file
            lock_file = open(lock_file_path, 'w')
            self._lock_files[lock_name] = lock_file

            # Try to acquire exclusive lock (non-blocking first)
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                logger.debug(f"Acquired file lock: {lock_name} at {lock_file_path}")
                return True
            except (IOError, OSError) as e:
                # Lock is held by another process
                logger.debug(f"Lock {lock_name} is held by another process, waiting...")

                # If timeout is set, wait for the lock
                if self.timeout > 0:
                    import time
                    start_time = time.time()

                    while time.time() - start_time < self.timeout:
                        try:
                            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                            logger.debug(f"Acquired file lock after waiting: {lock_name}")
                            return True
                        except (IOError, OSError):
                            time.sleep(0.1)  # Wait 100ms before retry

                    # Timeout reached
                    logger.error(f"Timeout waiting for file lock: {lock_name}")
                    lock_file.close()
                    del self._lock_files[lock_name]
                    return False
                else:
                    # No timeout, fail immediately
                    logger.error(f"Failed to acquire file lock {lock_name}: lock is held")
                    lock_file.close()
                    del self._lock_files[lock_name]
                    return False

        except Exception as e:
            logger.error(f"Failed to acquire file lock {lock_name}: {e}")
            # Clean up if we opened the file
            if lock_name in self._lock_files:
                try:
                    self._lock_files[lock_name].close()
                    del self._lock_files[lock_name]
                except Exception:
                    pass
            return False

    def release(self, session: Session, lock_name: str) -> bool:
        """Release file-based lock.

        Releases the exclusive lock on the file and closes the file handle.

        Args:
            session: SQLAlchemy session (not used for file locks)
            lock_name: Name of the lock

        Returns:
            True if lock released successfully, False otherwise
        """
        if lock_name not in self._lock_files:
            logger.warning(f"Lock {lock_name} not found in tracked locks")
            return True

        try:
            lock_file = self._lock_files[lock_name]

            # Release the lock
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
            lock_file.close()

            del self._lock_files[lock_name]
            logger.debug(f"Released file lock: {lock_name}")
            return True

        except Exception as e:
            logger.error(f"Failed to release file lock {lock_name}: {e}")
            return False

    def is_locked(self, session: Session, lock_name: str) -> bool:
        """Check if the current process holds the lock.

        Args:
            session: SQLAlchemy session (not used for file locks)
            lock_name: Name of the lock

        Returns:
            True if the current process holds the lock
        """
        return lock_name in self._lock_files

    def __del__(self):
        """Cleanup: release all open locks on destruction."""
        for lock_name in list(self._lock_files.keys()):
            try:
                lock_file = self._lock_files[lock_name]
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
                lock_file.close()
            except Exception:
                pass
        self._lock_files.clear()

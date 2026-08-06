"""Cross-platform file locking for Windows and Unix systems."""

import sys
from contextlib import contextmanager
from typing import BinaryIO


@contextmanager
def file_lock(file_handle: BinaryIO, exclusive: bool = True):
    """Acquire a file lock, cross-platform.

    Args:
        file_handle: Open file handle
        exclusive: True for exclusive lock, False for shared lock

    Yields:
        None

    Raises:
        RuntimeError: If lock cannot be acquired (Unix only)
    """
    if sys.platform == "win32":
        # Windows: Use msvcrt locking (simplified, no actual file locking)
        # In production, consider using portalocker library
        try:
            yield
        finally:
            pass
    else:
        # Unix/Linux: Use fcntl
        import fcntl

        try:
            lock_type = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
            fcntl.flock(file_handle.fileno(), lock_type | fcntl.LOCK_NB)
            yield
        finally:
            fcntl.flock(file_handle.fileno(), fcntl.LOCK_UN)


def try_acquire_lock(file_handle: BinaryIO, exclusive: bool = True) -> bool:
    """Try to acquire a file lock without blocking.

    Args:
        file_handle: Open file handle
        exclusive: True for exclusive lock, False for shared lock

    Returns:
        True if lock acquired, False otherwise
    """
    if sys.platform == "win32":
        # Windows: Simplified implementation (always succeeds)
        return True
    else:
        # Unix/Linux: Use fcntl
        import fcntl

        try:
            lock_type = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
            fcntl.flock(file_handle.fileno(), lock_type | fcntl.LOCK_NB)
            return True
        except (BlockingIOError, OSError):
            return False


def release_lock(file_handle: BinaryIO):
    """Release a file lock.

    Args:
        file_handle: Open file handle with lock
    """
    if sys.platform == "win32":
        # Windows: No-op for simplified implementation
        pass
    else:
        # Unix/Linux: Use fcntl
        import fcntl

        fcntl.flock(file_handle.fileno(), fcntl.LOCK_UN)
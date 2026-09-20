"""Small cross-platform coordination-file lock adapter.

POSIX keeps the existing ``flock`` behavior. Windows uses a one-byte OS lock in
the otherwise content-free lock file so callers retain the same process-level
blocking and non-blocking semantics without a third-party dependency.
"""

from __future__ import annotations

import os
from typing import Any, IO

if os.name == "nt":
    import ctypes
    import msvcrt
    from ctypes import wintypes
else:
    import fcntl


if os.name == "nt":
    _LOCKFILE_FAIL_IMMEDIATELY = 0x00000001
    _LOCKFILE_EXCLUSIVE_LOCK = 0x00000002
    _ERROR_LOCK_VIOLATION = 33

    class _Overlapped(ctypes.Structure):
        _fields_ = [
            ("Internal", ctypes.c_size_t),
            ("InternalHigh", ctypes.c_size_t),
            ("Offset", wintypes.DWORD),
            ("OffsetHigh", wintypes.DWORD),
            ("hEvent", wintypes.HANDLE),
        ]

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _lock_file_ex = _kernel32.LockFileEx
    _lock_file_ex.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(_Overlapped),
    ]
    _lock_file_ex.restype = wintypes.BOOL
    _unlock_file_ex = _kernel32.UnlockFileEx
    _unlock_file_ex.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(_Overlapped),
    ]
    _unlock_file_ex.restype = wintypes.BOOL


def _prepare_windows_lock_byte(handle: IO[Any]) -> None:
    """Ensure byte zero exists and select it as the lock region."""

    descriptor = handle.fileno()
    if os.fstat(descriptor).st_size == 0:
        os.lseek(descriptor, 0, os.SEEK_SET)
        os.write(descriptor, b"\0")
    os.lseek(descriptor, 0, os.SEEK_SET)


def lock_file(
    handle: IO[Any],
    *,
    exclusive: bool = True,
    blocking: bool = True,
) -> None:
    """Acquire a whole coordination-file lock for an open handle."""

    if os.name != "nt":
        operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        if not blocking:
            operation |= fcntl.LOCK_NB
        fcntl.flock(handle.fileno(), operation)
        return

    _prepare_windows_lock_byte(handle)
    flags = _LOCKFILE_EXCLUSIVE_LOCK if exclusive else 0
    if not blocking:
        flags |= _LOCKFILE_FAIL_IMMEDIATELY
    overlapped = _Overlapped()
    acquired = _lock_file_ex(
        msvcrt.get_osfhandle(handle.fileno()),
        flags,
        0,
        1,
        0,
        ctypes.byref(overlapped),
    )
    if acquired:
        return
    error_code = ctypes.get_last_error()
    if not blocking and error_code == _ERROR_LOCK_VIOLATION:
        raise BlockingIOError(
            error_code,
            "lock file is already held by another process",
        )
    raise ctypes.WinError(error_code)


def unlock_file(handle: IO[Any]) -> None:
    """Release a lock previously acquired with :func:`lock_file`."""

    if os.name != "nt":
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        return

    os.lseek(handle.fileno(), 0, os.SEEK_SET)
    overlapped = _Overlapped()
    released = _unlock_file_ex(
        msvcrt.get_osfhandle(handle.fileno()),
        0,
        1,
        0,
        ctypes.byref(overlapped),
    )
    if not released:
        raise ctypes.WinError(ctypes.get_last_error())

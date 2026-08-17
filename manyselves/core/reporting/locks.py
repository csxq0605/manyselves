"""Cross-process locks for writers of shared reporting outputs."""

from __future__ import annotations

import fcntl
import os
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import IO, Iterator


class ReportingWriterBusyError(RuntimeError):
    """Raised when another process owns the workspace reporting writer lock."""


@contextmanager
def exclusive_reporting_writer_lock(workspace: Path) -> Iterator[IO[str]]:
    """Hold the non-blocking, workspace-wide reporting single-writer lock.

    The lock protects shared ``Work/*`` and ``Outputs/*`` publication. Callers
    that also need a run lock must acquire this workspace lock first so every
    writer follows one lock order.
    """

    workspace_root = Path(workspace).resolve()
    work_root = workspace_root / "Work"
    work_root.mkdir(parents=True, exist_ok=True)
    if work_root.is_symlink() or not work_root.resolve().is_relative_to(
        workspace_root
    ):
        raise ValueError("reporting lock root must stay inside the workspace")

    lock_path = work_root / ".reporting-writer.lock"
    if lock_path.is_symlink():
        raise ValueError("reporting writer lock must not be a symbolic link")

    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        raise ValueError("unable to open reporting writer lock safely") from exc

    handle = os.fdopen(descriptor, "a+", encoding="utf-8")
    try:
        opened_stat = os.fstat(handle.fileno())
        try:
            path_stat = os.lstat(lock_path)
        except OSError as exc:
            raise ValueError(
                "reporting writer lock identity cannot be verified"
            ) from exc
        if (
            not stat.S_ISREG(opened_stat.st_mode)
            or not stat.S_ISREG(path_stat.st_mode)
            or opened_stat.st_nlink != 1
            or path_stat.st_nlink != 1
            or (opened_stat.st_dev, opened_stat.st_ino)
            != (path_stat.st_dev, path_stat.st_ino)
        ):
            raise ValueError(
                "reporting writer lock must be one verified regular file"
            )
        os.fchmod(handle.fileno(), 0o600)
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ReportingWriterBusyError(
                "another report writer is active in this workspace"
            ) from exc
        try:
            locked_stat = os.lstat(lock_path)
            if (
                not stat.S_ISREG(locked_stat.st_mode)
                or locked_stat.st_nlink != 1
                or (opened_stat.st_dev, opened_stat.st_ino)
                != (locked_stat.st_dev, locked_stat.st_ino)
            ):
                raise ValueError(
                    "reporting writer lock identity changed while acquiring the lock"
                )
            yield handle
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()

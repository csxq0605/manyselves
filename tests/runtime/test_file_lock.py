from pathlib import Path

import pytest

from manyselves.runtime.file_lock import lock_file, unlock_file


def _open_lock(path: Path):
    return path.open("a+", encoding="utf-8")


def test_nonblocking_exclusive_lock_reports_contention(tmp_path: Path) -> None:
    path = tmp_path / "exclusive.lock"
    with _open_lock(path) as owner, _open_lock(path) as contender:
        lock_file(owner)
        try:
            with pytest.raises(BlockingIOError):
                lock_file(contender, blocking=False)
        finally:
            unlock_file(owner)

        lock_file(contender, blocking=False)
        unlock_file(contender)


def test_shared_locks_coexist_and_exclude_writer(tmp_path: Path) -> None:
    path = tmp_path / "shared.lock"
    with (
        _open_lock(path) as first_reader,
        _open_lock(path) as second_reader,
        _open_lock(path) as writer,
    ):
        lock_file(first_reader, exclusive=False, blocking=False)
        try:
            lock_file(second_reader, exclusive=False, blocking=False)
            try:
                with pytest.raises(BlockingIOError):
                    lock_file(writer, blocking=False)
            finally:
                unlock_file(second_reader)
        finally:
            unlock_file(first_reader)

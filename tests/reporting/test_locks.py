"""Workspace writer-lock contract tests."""

import stat
from pathlib import Path

import pytest

from manyselves.core.reporting.locks import (
    ReportingWriterBusyError,
    exclusive_reporting_writer_lock,
)


def test_workspace_lock_is_nonblocking_and_releases(tmp_path: Path) -> None:
    with exclusive_reporting_writer_lock(tmp_path) as handle:
        assert handle is not None
        lock_path = tmp_path / "Work/.reporting-writer.lock"
        assert stat.S_IMODE(lock_path.stat().st_mode) == 0o600
        with pytest.raises(
            ReportingWriterBusyError,
            match="another report writer is active",
        ):
            with exclusive_reporting_writer_lock(tmp_path):
                pass

    with exclusive_reporting_writer_lock(tmp_path):
        pass


def test_workspace_lock_rejects_a_symlink_lock_file(tmp_path: Path) -> None:
    work = tmp_path / "Work"
    work.mkdir()
    outside = tmp_path / "outside.lock"
    outside.write_text("untouched", encoding="utf-8")
    (work / ".reporting-writer.lock").symlink_to(outside)

    with pytest.raises(ValueError, match="must not be a symbolic link"):
        with exclusive_reporting_writer_lock(tmp_path):
            pass

    assert outside.read_text(encoding="utf-8") == "untouched"


def test_workspace_lock_rejects_hardlinks_before_chmod(tmp_path: Path) -> None:
    outside = tmp_path / "outside.lock"
    outside.write_text("untouched", encoding="utf-8")
    outside.chmod(0o644)
    original_mode = stat.S_IMODE(outside.stat().st_mode)
    work = tmp_path / "Work"
    work.mkdir()
    (work / ".reporting-writer.lock").hardlink_to(outside)

    with pytest.raises(ValueError, match="one verified regular file"):
        with exclusive_reporting_writer_lock(tmp_path):
            pass

    assert outside.read_text(encoding="utf-8") == "untouched"
    assert stat.S_IMODE(outside.stat().st_mode) == original_mode

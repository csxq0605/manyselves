"""Release-level traceability and representative recovery drills."""

from __future__ import annotations

import ast
import asyncio
import errno
from pathlib import Path

import pytest

from manyselves.application.workspace_files import (
    FileRevisionConflict,
    WorkspaceFiles,
)

RECOVERY_EVIDENCE = {
    "provider_timeout": "tests/webapi/test_security_and_control.py::test_internal_errors_use_a_non_secret_error_envelope",
    "provider_429": "tests/webapi/test_security_and_control.py::test_http_errors_use_the_error_envelope",
    "disk_full": "tests/application/test_workspace_files.py::test_write_is_atomic_and_cleans_failed_sibling_temp",
    "upload_disconnect": "tests/application/test_workspace_files.py::test_interrupted_upload_cleans_temp_and_destination",
    "runtime_shutdown": "tests/webapi/test_conversations_agents_reporting.py::test_failed_normal_shutdown_blocks_new_runtime_until_cleanup_finishes",
    "stale_revision": "tests/application/test_workspace_files.py::test_write_rejects_stale_revision",
    "sse_cursor_evicted": "tests/webapi/test_sse.py::test_register_replays_present_cursor_or_emits_non_replayed_resync",
    "report_waiting_user": "tests/reporting/test_service_boundary.py::test_service_retains_same_identity_registry_while_waiting_for_user",
    "invalid_checkpoint": "tests/webapi/test_conversations_agents_reporting.py::test_missing_checkpoint_fails_before_durable_truncation",
}


@pytest.mark.parametrize(("fault", "node_id"), RECOVERY_EVIDENCE.items())
def test_every_release_fault_has_executable_regression_evidence(fault: str, node_id: str) -> None:
    """Keep the release fault matrix bound to concrete tests collected by Gate E."""
    path_text, function_name = node_id.split("::", 1)
    path = Path(path_text)
    assert path.is_file(), fault
    functions = {
        node.name
        for node in ast.parse(path.read_text(encoding="utf-8")).body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert function_name in functions, f"{fault}: {node_id}"


def test_disk_full_preserves_authoritative_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "workspace"
    (root / "Inputs").mkdir(parents=True)
    target = root / "Inputs" / "brief.txt"
    target.write_text("authoritative", encoding="utf-8")
    files = WorkspaceFiles(root)
    revision = files.read_text("Inputs/brief.txt").revision

    def disk_full(_self: Path, _target: Path) -> Path:
        raise OSError(errno.ENOSPC, "injected disk full")

    monkeypatch.setattr(Path, "replace", disk_full)
    with pytest.raises(OSError) as error:
        files.write_text("Inputs/brief.txt", "partial", revision)

    assert error.value.errno == errno.ENOSPC
    assert target.read_text(encoding="utf-8") == "authoritative"
    assert not list(target.parent.glob(".manyselves-tmp-*"))


@pytest.mark.asyncio
async def test_upload_disconnect_leaves_no_partial_authoritative_file(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    (root / "Inputs").mkdir(parents=True)
    files = WorkspaceFiles(root)

    async def disconnected():
        yield b"partial"
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await files.upload("Inputs/upload.bin", disconnected())

    assert not (root / "Inputs" / "upload.bin").exists()
    assert not list((root / "Inputs").glob(".manyselves-tmp-*"))


def test_stale_revision_is_recoverable_without_overwrite(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    (root / "Inputs").mkdir(parents=True)
    (root / "Inputs" / "brief.txt").write_text("one", encoding="utf-8")
    files = WorkspaceFiles(root)
    stale = files.read_text("Inputs/brief.txt").revision
    current = files.write_text("Inputs/brief.txt", "two", stale)

    with pytest.raises(FileRevisionConflict):
        files.write_text("Inputs/brief.txt", "lost update", stale)

    assert files.read_text("Inputs/brief.txt") == current

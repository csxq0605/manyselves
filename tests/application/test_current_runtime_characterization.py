"""Characterize the PyQt runtime boundary before Phase 1 extraction."""

from __future__ import annotations

import asyncio
import csv
import re
import subprocess
import sys
from pathlib import Path

import pytest

from manyselves.app import BackendAPIImpl
from manyselves.config import ConfigManager
from manyselves.core.project_structure import ensure_project_structure
from manyselves.interfaces.types import UserMessage
from manyselves.runtime.loops import MessageBus

CHECKER = Path(__file__).resolve().parents[2] / "scripts" / "check_phase1_core_freeze.py"
REPO_ROOT = CHECKER.parent.parent
PARITY_CSV = REPO_ROOT / "docs" / "phase1" / "feature-parity.csv"
PARITY_HEADER = [
    "id",
    "module",
    "feature",
    "legacy_evidence",
    "api_or_bridge",
    "react_evidence",
    "browser_test",
    "electron_test",
    "status",
    "reviewer",
    "rationale",
]
REQUIRED_PARITY_FAMILIES = {
    "PROJECT",
    "FILE",
    "EDITOR",
    "PREVIEW",
    "PYTHON",
    "CONV",
    "MESSAGE",
    "AGENT",
    "TOOL",
    "TASK",
    "CONFIG",
    "REPORT",
    "DESKTOP",
    "RECOVERY",
    "DEPLOY",
}
REQUIRED_PARITY_IDS = {
    "FILE-007",
    "FILE-008",
    "FILE-009",
    "FILE-010",
    "FILE-011",
    "EDITOR-006",
    "EDITOR-007",
    "EDITOR-008",
    "PREVIEW-006",
    "PREVIEW-007",
    "PREVIEW-008",
    "PREVIEW-009",
    "PREVIEW-010",
    "CONV-006",
    "MESSAGE-006",
    "AGENT-006",
    "AGENT-007",
    "AGENT-008",
    "AGENT-009",
    "AGENT-010",
    "AGENT-011",
    "REPORT-004",
    "REPORT-005",
    "REPORT-006",
    "REPORT-007",
    "REPORT-008",
    "REPORT-009",
    "REPORT-010",
    "DESKTOP-005",
    "DESKTOP-006",
    "DESKTOP-007",
    "DEPLOY-001",
    "DEPLOY-002",
    "DEPLOY-003",
}
REQUIRED_PARITY_EVIDENCE = {
    "REPORT-010": (
        "tests/capabilities/distribution_reporting/test_delivery_tools.py::test_publish_materialize_is_capability_owned_for_serialized_delivery_context",
    ),
    "DEPLOY-001": (
        "docs/superpowers/plans/2026-07-31-manyselves-phase1-05-electron-deployment.md",
        "Task 1 Hardened Electron Shell",
    ),
    "DEPLOY-002": (
        "docs/superpowers/plans/2026-07-31-manyselves-phase1-05-electron-deployment.md",
        "Task 4 Build Nginx Web Image and Compose Topology",
    ),
    "DEPLOY-003": (
        "docs/superpowers/plans/2026-07-31-manyselves-phase1-05-electron-deployment.md",
        "Task 5 Add Backup Restore Operations Packaging and Gate D",
    ),
    "DESKTOP-005": ("manyselves/gui/main_window.py:MainWindow._on_new_window",),
    "DESKTOP-006": (
        "manyselves/gui/main_window.py:MainWindow._on_new_file _on_new_folder and _on_open_folder",
    ),
    "DESKTOP-007": ("manyselves/gui/main_window.py:MainWindow._on_open_file",),
}
REQUIRED_PARITY_FEATURE_TERMS = {
    "DESKTOP-005": ("unsupported", "multi-window"),
    "DESKTOP-006": ("native", "file", "folder"),
    "DESKTOP-007": ("unsupported", "open-file"),
}
REQUIRED_RUNTIME_EVIDENCE = {
    "RUNTIME-001": (
        "manyselves/app.py:ManyselvesApp.startup",
        "manyselves/application/runtime_host.py:RuntimeHost.start",
        "tests/application/test_pyqt_runtime_compatibility.py::test_desktop_startup_preserves_boolean_and_loop_manager",
    ),
    "RUNTIME-002": (
        "manyselves/app.py:ManyselvesApp.shutdown",
        "manyselves/application/runtime_host.py:RuntimeHost.stop",
        "tests/application/test_pyqt_runtime_compatibility.py::test_desktop_shutdown_preserves_owned_cleanup_boundary",
    ),
    "RUNTIME-003": (
        "manyselves/application/backend_api.py:BackendAPIImpl.send_user_message",
        "manyselves/application/runtime_facade.py:RuntimeFacade.send_user_message",
        "tests/application/test_pyqt_runtime_compatibility.py::test_facade_send_matches_direct_backend_observable_message",
    ),
    "RUNTIME-004": (
        "manyselves/application/backend_api.py:BackendAPIImpl.interrupt_current_message",
        "manyselves/application/runtime_facade.py:RuntimeFacade.interrupt",
        "tests/application/test_pyqt_runtime_compatibility.py::test_facade_interrupt_matches_direct_backend_target",
    ),
    "RUNTIME-005": (
        "manyselves/application/backend_api.py:BackendAPIImpl.rollback_to_checkpoint",
        "manyselves/application/runtime_facade.py:RuntimeFacade.rollback",
        "tests/application/test_pyqt_runtime_compatibility.py::test_facade_rollback_matches_direct_backend_result",
    ),
    "RUNTIME-006": (
        "manyselves/application/backend_api.py:BackendAPIImpl.set_agent_debug_mode",
        "manyselves/application/backend_api.py:BackendAPIImpl.set_agent_debug_mode",
        "tests/application/test_pyqt_runtime_compatibility.py::test_direct_backend_debug_preserves_loop_manager_behavior",
    ),
    "RUNTIME-007": (
        "manyselves/application/backend_api.py:BackendAPIImpl.switch_provider",
        "manyselves/application/backend_api.py:BackendAPIImpl.switch_provider",
        "tests/application/test_pyqt_runtime_compatibility.py::test_provider_switch_assigns_provider_and_requests_restart",
    ),
    "RUNTIME-008": (
        "manyselves/application/backend_api.py:BackendAPIImpl.switch_model",
        "manyselves/application/backend_api.py:BackendAPIImpl.switch_model",
        "tests/application/test_pyqt_runtime_compatibility.py::test_model_switch_assigns_model_without_restart_request",
    ),
}
EVIDENCE_PATH = re.compile(r"(?:manyselves|tests|docs)/[A-Za-z0-9_./-]+")


async def _append_async(messages: list[UserMessage], message: UserMessage) -> None:
    messages.append(message)


async def _wait_until(predicate, timeout: float = 1.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() >= deadline:
            raise AssertionError("condition was not met before timeout")
        await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_backend_publishes_user_message_with_existing_mapping() -> None:
    bus = MessageBus()
    api = BackendAPIImpl(ConfigManager(), bus)
    seen: list[UserMessage] = []

    async def on_user_message(message: UserMessage) -> None:
        await _append_async(seen, message)

    bus.subscribe(UserMessage, on_user_message)
    processor = asyncio.create_task(bus.process_queue())

    await api.send_user_message("hello", "sub", message_id="m-1")
    await _wait_until(lambda: len(seen) == 1)
    bus.shutdown()
    await processor

    assert seen[0].agent_type == "main"
    assert seen[0].message_id == "m-1"


def test_project_startup_uses_canonical_structure(tmp_path: Path) -> None:
    ensure_project_structure(tmp_path)

    assert {"Inputs", "Knowledge", "Templates", "Work", "Outputs"} <= {
        child.name for child in tmp_path.iterdir()
    }


def test_feature_parity_matrix_is_a_complete_planned_inventory() -> None:
    with PARITY_CSV.open(encoding="utf-8", newline="") as csv_file:
        reader = csv.DictReader(csv_file)
        rows = list(reader)

    assert reader.fieldnames == PARITY_HEADER
    assert rows
    assert all(row["id"] and row["feature"] for row in rows)
    assert len({row["id"] for row in rows}) == len(rows)
    assert REQUIRED_PARITY_FAMILIES <= {row["module"] for row in rows}
    assert REQUIRED_PARITY_IDS <= {row["id"] for row in rows}
    assert all(row["legacy_evidence"] and row["rationale"] for row in rows)

    for row in rows:
        cited_paths = EVIDENCE_PATH.findall(row["legacy_evidence"])
        assert cited_paths, f"{row['id']} has no repository evidence path"
        assert all((REPO_ROOT / path).is_file() for path in cited_paths)

    rows_by_id = {row["id"]: row for row in rows}
    for capability_id, anchors in REQUIRED_PARITY_EVIDENCE.items():
        assert all(anchor in rows_by_id[capability_id]["legacy_evidence"] for anchor in anchors)
    for capability_id, terms in REQUIRED_PARITY_FEATURE_TERMS.items():
        feature = rows_by_id[capability_id]["feature"].lower()
        assert all(term in feature for term in terms)

    runtime_rows = [row for row in rows if row["module"] == "RUNTIME"]
    assert {row["id"] for row in runtime_rows} == set(REQUIRED_RUNTIME_EVIDENCE)
    assert all(row["status"] == "accepted" for row in runtime_rows)
    assert all(
        not row["react_evidence"]
        and not row["browser_test"]
        and not row["electron_test"]
        for row in runtime_rows
    )
    assert all(
        "foundation" in row["rationale"].lower()
        and "pending" in row["rationale"].lower()
        for row in runtime_rows
    )
    api_rows = [row for row in rows if row["module"] == "API"]
    sse_rows = [row for row in rows if row["module"] == "SSE"]
    assert api_rows and sse_rows
    assert all(row["status"] == "accepted" for row in [*api_rows, *sse_rows])
    assert all(
        not row["react_evidence"]
        and not row["browser_test"]
        and not row["electron_test"]
        for row in api_rows
    )
    assert all(
        row["react_evidence"]
        and row["browser_test"]
        and not row["electron_test"]
        for row in sse_rows
    )
    assert all(
        row["status"] == "tested"
        for row in rows
        if row["module"] not in {"RUNTIME", "API", "SSE"}
    )
    for capability_id, anchors in REQUIRED_RUNTIME_EVIDENCE.items():
        combined_evidence = " ".join(
            (
                rows_by_id[capability_id]["legacy_evidence"],
                rows_by_id[capability_id]["api_or_bridge"],
            )
        )
        assert all(anchor in combined_evidence for anchor in anchors)


def _run_git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        text=True,
        capture_output=True,
        check=True,
    )


def init_repo(repo: Path, files: dict[str, str]) -> Path:
    repo.mkdir()
    _run_git(repo, "init")
    _run_git(repo, "config", "user.email", "tests@example.com")
    _run_git(repo, "config", "user.name", "Freeze Checker Test")
    for relative_path, content in files.items():
        path = repo / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    _run_git(repo, "add", ".")
    _run_git(repo, "commit", "-m", "baseline")
    return repo


def git_head(repo: Path) -> str:
    return _run_git(repo, "rev-parse", "HEAD").stdout.strip()


def run_checker(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(CHECKER), *args],
        cwd=repo,
        text=True,
        capture_output=True,
        check=False,
    )


def test_freeze_checker_rejects_protected_change(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo", {"manyselves/core/sample.py": "before\n"})
    baseline_file = repo / "core-freeze-base.txt"
    baseline_file.write_text(git_head(repo) + "\n", encoding="utf-8")
    (repo / "manyselves/core/sample.py").write_text("after\n", encoding="utf-8")

    result = run_checker(repo, "--base-file", str(baseline_file))

    assert result.returncode == 1
    assert "manyselves/core/sample.py" in result.stdout


def test_freeze_checker_allows_unprotected_change(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo", {"manyselves/core/sample.py": "before\n"})
    baseline_file = repo / "core-freeze-base.txt"
    baseline_file.write_text(git_head(repo) + "\n", encoding="utf-8")
    (repo / "manyselves/application.py").write_text("after\n", encoding="utf-8")

    result = run_checker(repo, "--base-file", str(baseline_file))

    assert result.returncode == 0
    assert result.stdout == ""


def test_freeze_checker_rejects_missing_or_invalid_baseline(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo", {"manyselves/core/sample.py": "before\n"})
    baseline_file = repo / "core-freeze-base.txt"

    missing = run_checker(repo, "--base-file", str(baseline_file))
    baseline_file.write_text("not-a-commit\n", encoding="utf-8")
    invalid = run_checker(repo, "--base-file", str(baseline_file))

    assert missing.returncode == 2
    assert invalid.returncode == 2


def test_freeze_checker_rejects_symbolic_or_abbreviated_baseline(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo", {"manyselves/core/sample.py": "before\n"})
    baseline_file = repo / "core-freeze-base.txt"
    baseline_file.write_text("HEAD\n", encoding="utf-8")

    symbolic = run_checker(repo, "--base-file", str(baseline_file))
    baseline_file.write_text(git_head(repo)[:12] + "\n", encoding="utf-8")
    abbreviated = run_checker(repo, "--base-file", str(baseline_file))

    assert symbolic.returncode == 2
    assert abbreviated.returncode == 2


def test_freeze_checker_initializes_baseline_once(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo", {"manyselves/core/sample.py": "before\n"})
    baseline_file = repo / "core-freeze-base.txt"

    first = run_checker(repo, "--initialize", str(baseline_file))
    second = run_checker(repo, "--initialize", str(baseline_file))

    assert first.returncode == 0
    assert baseline_file.read_text(encoding="utf-8") == git_head(repo) + "\n"
    assert second.returncode == 2

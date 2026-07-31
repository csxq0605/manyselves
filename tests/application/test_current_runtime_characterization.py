"""Characterize the PyQt runtime boundary before Phase 1 extraction."""

from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path

import pytest

from manyselves.app import BackendAPIImpl
from manyselves.config import ConfigManager
from manyselves.core.loops import MessageBus
from manyselves.core.project_structure import ensure_project_structure
from manyselves.interfaces.types import UserMessage

CHECKER = Path(__file__).resolve().parents[2] / "scripts" / "check_phase1_core_freeze.py"


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


def test_freeze_checker_initializes_baseline_once(tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "repo", {"manyselves/core/sample.py": "before\n"})
    baseline_file = repo / "core-freeze-base.txt"

    first = run_checker(repo, "--initialize", str(baseline_file))
    second = run_checker(repo, "--initialize", str(baseline_file))

    assert first.returncode == 0
    assert baseline_file.read_text(encoding="utf-8") == git_head(repo) + "\n"
    assert second.returncode == 2

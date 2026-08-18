"""Isolation contracts for the server-managed global knowledge library."""

from pathlib import Path

import pytest

from manyselves.application.global_knowledge_service import GlobalKnowledgeService
from manyselves.application.workspace_files import UnsafeWorkspacePath


def test_service_root_is_hidden_global_directory(tmp_path: Path) -> None:
    service = GlobalKnowledgeService.from_data_root(
        tmp_path,
        max_text_bytes=2 * 1024 * 1024,
        max_upload_bytes=100 * 1024 * 1024,
        max_tree_entries=20_000,
    )

    assert service.root == (tmp_path / ".manyselves" / "global-knowledge").resolve()
    assert service.root.is_dir()
    assert not (tmp_path / "Inputs").exists()
    assert not (tmp_path / "Knowledge").exists()


def test_service_reuses_revision_safe_relative_file_operations(tmp_path: Path) -> None:
    service = GlobalKnowledgeService.from_data_root(tmp_path)

    created = service.create_file("rule.md", "first")
    current = service.read_text("rule.md")
    saved = service.write_text("rule.md", "second", current.revision)

    assert created.path == "rule.md"
    assert saved.content == "second"
    assert [entry.path for entry in service.list_tree()] == ["rule.md"]


@pytest.mark.parametrize("path", ["../project/Knowledge/rule.md", "/tmp/rule.md", r"C:\rule.md"])
def test_service_rejects_paths_outside_the_global_root(tmp_path: Path, path: str) -> None:
    service = GlobalKnowledgeService.from_data_root(tmp_path)

    with pytest.raises(UnsafeWorkspacePath):
        service.resolve(path)


def test_service_rejects_a_symlinked_hidden_parent(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    hidden = tmp_path / ".manyselves"
    try:
        hidden.symlink_to(outside, target_is_directory=True)
    except OSError as error:
        pytest.skip(f"platform denied symlink creation: {error}")

    with pytest.raises(UnsafeWorkspacePath):
        GlobalKnowledgeService.from_data_root(tmp_path)

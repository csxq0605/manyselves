"""Behavioral contracts for filesystem-backed project display metadata."""

import hashlib
import importlib
from pathlib import Path

import pytest


def _metadata_contract():
    """Fail red, rather than error during collection, until the new module exists."""
    try:
        return importlib.import_module("manyselves.application.project_metadata")
    except ModuleNotFoundError:
        pytest.fail("project metadata contract is not implemented")


def test_read_missing_metadata_falls_back_to_stable_project_id(tmp_path: Path) -> None:
    """Legacy project directories must remain displayable before they gain a sidecar."""
    project_root = tmp_path / "legacy-project"
    project_root.mkdir()

    contract = _metadata_contract()
    metadata = contract.ProjectMetadataStore().read(project_root)

    assert metadata == contract.ProjectMetadata(display_name="legacy-project", description="")


def test_write_trims_display_name_and_returns_metadata_file_revision(tmp_path: Path) -> None:
    """Removing normalized data or hashing a non-persisted value would break optimistic clients."""
    project_root = tmp_path / "analysis"
    project_root.mkdir()
    contract = _metadata_contract()

    revision = contract.ProjectMetadataStore().write(
        project_root,
        contract.ProjectMetadata(display_name="  Analysis workspace  ", description="Quarterly review"),
        revision=None,
    )
    metadata_path = project_root / ".manyselves" / "project.json"

    assert contract.ProjectMetadataStore().read(project_root) == contract.ProjectMetadata(
        display_name="Analysis workspace", description="Quarterly review"
    )
    assert revision == hashlib.sha256(metadata_path.read_bytes()).hexdigest()


@pytest.mark.parametrize(
    "metadata",
    [
        ("   ", "valid"),
        ("x" * 121, "valid"),
        ("valid", "x" * 1001),
    ],
)
def test_write_rejects_invalid_display_metadata(
    tmp_path: Path, metadata: tuple[str, str]
) -> None:
    """Invalid values must not create a metadata sidecar that clients later cannot edit."""
    project_root = tmp_path / "analysis"
    project_root.mkdir()
    contract = _metadata_contract()

    with pytest.raises(contract.InvalidProjectMetadata):
        contract.ProjectMetadataStore().write(
            project_root,
            contract.ProjectMetadata(display_name=metadata[0], description=metadata[1]),
            revision=None,
        )

    assert not (project_root / ".manyselves" / "project.json").exists()


def test_create_rolls_back_invalid_metadata_and_allows_a_retry(tmp_path: Path) -> None:
    """Leaving a failed create directory behind would turn a corrected retry into a false conflict."""
    contract = _metadata_contract()
    registry_module = importlib.import_module("manyselves.application.project_registry")
    registry = registry_module.ProjectRegistry(tmp_path, "primary")
    project_root = tmp_path / "new-project"

    with pytest.raises(contract.InvalidProjectMetadata):
        registry.create(
            "new-project",
            contract.ProjectMetadata(display_name="x" * 121, description=""),
        )

    assert not project_root.exists()
    retried = registry.create(
        "new-project",
        contract.ProjectMetadata(display_name="Retry succeeds", description=""),
    )
    assert retried.id == "new-project"
    assert project_root.is_dir()


def test_write_rejects_stale_revision_without_overwriting_current_metadata(tmp_path: Path) -> None:
    """Dropping the revision comparison would let a stale pencil edit overwrite a newer one."""
    project_root = tmp_path / "analysis"
    project_root.mkdir()
    contract = _metadata_contract()
    store = contract.ProjectMetadataStore()
    revision = store.write(
        project_root,
        contract.ProjectMetadata(display_name="Initial", description=""),
        revision=None,
    )
    store.write(
        project_root,
        contract.ProjectMetadata(display_name="Current", description="Current description"),
        revision=revision,
    )

    with pytest.raises(contract.ProjectMetadataRevisionConflict):
        store.write(
            project_root,
            contract.ProjectMetadata(display_name="Stale", description="Stale description"),
            revision=revision,
        )

    assert store.read(project_root) == contract.ProjectMetadata(
        display_name="Current", description="Current description"
    )


def test_read_rejects_a_symlinked_metadata_path(tmp_path: Path) -> None:
    """Following a metadata symlink would let a project expose data outside its workspace."""
    project_root = tmp_path / "analysis"
    project_root.mkdir()
    contract = _metadata_contract()
    outside = tmp_path / "outside.json"
    outside.write_text('{"displayName": "outside", "description": ""}', encoding="utf-8")
    metadata_dir = project_root / ".manyselves"
    metadata_dir.mkdir()
    metadata_path = metadata_dir / "project.json"
    try:
        metadata_path.symlink_to(outside)
    except OSError as error:
        pytest.skip(f"platform denied symlink creation: {error}")

    with pytest.raises(contract.UnsafeProjectMetadataPath):
        contract.ProjectMetadataStore().read(project_root)

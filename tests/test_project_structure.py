"""Tests for the Capability-neutral Manyselves project layout."""

from pathlib import Path

from manyselves.core.project_structure import ensure_project_structure


def _has_exact_path(root: Path, rel: str) -> bool:
    current = root
    for part in Path(rel).parts:
        matches = [child for child in current.iterdir() if child.name == part]
        if not matches:
            return False
        current = matches[0]
    return True


def test_ensure_project_structure_creates_canonical_case_only(tmp_path: Path) -> None:
    ensure_project_structure(tmp_path)

    for rel in (
        "Inputs",
        "Knowledge",
        "Templates",
        "Work/runs",
        "Outputs",
    ):
        assert _has_exact_path(tmp_path, rel)

    for rel in (
        "Outputs/Modules",
        "Outputs/Reviews",
        "Outputs/Reports",
        "Data",
        "References",
        "Theory",
        "Plots",
        "Outline",
        "Tex",
    ):
        assert not _has_exact_path(tmp_path, rel)


def test_ensure_project_structure_normalizes_current_directory_case(tmp_path: Path) -> None:
    (tmp_path / "inputs").mkdir()
    (tmp_path / "inputs" / "customer.xlsx").write_bytes(b"xlsx")
    (tmp_path / "outputs" / "reports").mkdir(parents=True)

    ensure_project_structure(tmp_path)

    assert (tmp_path / "Inputs" / "customer.xlsx").is_file()
    assert _has_exact_path(tmp_path, "Outputs/reports")
    assert not _has_exact_path(tmp_path, "inputs")
    assert not _has_exact_path(tmp_path, "outputs")

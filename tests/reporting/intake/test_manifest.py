import hashlib
from pathlib import Path

from docx import Document
from openpyxl import Workbook

from manyselves.capabilities.distribution_reporting.runtime.intake.manifest import (
    build_manifest,
)


def _write_workbook(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    workbook = Workbook()
    workbook.active.append(["测试"])
    workbook.save(path)


def test_manifest_classifies_three_core_workbooks_and_only_scans_inputs(
    tmp_path: Path,
) -> None:
    for filename in (
        "S2-1收资表.xlsx",
        "S4-4诊断工作用表.xlsx",
        "S4-6评估总表.xlsx",
    ):
        _write_workbook(tmp_path / "Inputs" / filename)
    _write_workbook(tmp_path / "Outputs" / "S4-4诊断工作用表.xlsx")
    _write_workbook(tmp_path / "项目根目录.xlsx")

    manifest = build_manifest(tmp_path)

    assert len(manifest.files) == 3
    assert {item.purpose for item in manifest.files} == {"s2-1", "s4-4", "s4-6"}
    assert [item.path.name for item in manifest.files] == sorted(
        item.path.name for item in manifest.files
    )
    assert all(item.path.parts[0] == "Inputs" for item in manifest.files)


def test_manifest_ids_and_hashes_are_stable(tmp_path: Path) -> None:
    path = tmp_path / "Inputs" / "S4-4诊断工作用表.xlsx"
    _write_workbook(path)

    first = build_manifest(tmp_path).files[0]
    second = build_manifest(tmp_path).files[0]

    assert first.id == second.id
    assert first.sha256 == hashlib.sha256(path.read_bytes()).hexdigest()
    assert first.parse_status == "pending"
    assert first.media_type.endswith("spreadsheetml.sheet")


def test_manifest_keeps_corrupt_workbook_for_isolated_parse_failure(tmp_path: Path) -> None:
    inputs = tmp_path / "Inputs"
    inputs.mkdir()
    (inputs / "S4-4诊断工作用表.xlsx").write_bytes(b"not-an-xlsx")
    _write_workbook(inputs / "S4-6评估总表.xlsx")

    manifest = build_manifest(tmp_path)

    assert [item.purpose for item in manifest.files] == ["s4-4", "s4-6"]


def test_manifest_includes_supported_and_manual_required_input_formats(tmp_path: Path) -> None:
    inputs = tmp_path / "Inputs"
    inputs.mkdir()
    for filename in ("说明.txt", "现场.md", "图纸.dwg", "视频.mp4"):
        (inputs / filename).write_bytes(b"content")
    Document().save(inputs / "检查.docx")

    manifest = build_manifest(tmp_path)

    assert {item.path.name for item in manifest.files} == {
        "说明.txt",
        "现场.md",
        "图纸.dwg",
        "视频.mp4",
        "检查.docx",
    }

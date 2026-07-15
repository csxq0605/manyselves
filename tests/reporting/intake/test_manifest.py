from pathlib import Path

from openpyxl import Workbook

from autoreport.core.reporting.intake.manifest import build_manifest


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
    assert all(item.path.parts[0] == "Inputs" for item in manifest.files)


def test_manifest_ids_and_hashes_are_stable(tmp_path: Path) -> None:
    path = tmp_path / "Inputs" / "S4-4诊断工作用表.xlsx"
    _write_workbook(path)

    first = build_manifest(tmp_path).files[0]
    second = build_manifest(tmp_path).files[0]

    assert first.id == second.id
    assert len(first.sha256) == 64
    assert first.parse_status == "pending"
    assert first.media_type.endswith("spreadsheetml.sheet")


def test_manifest_keeps_corrupt_workbook_for_isolated_parse_failure(tmp_path: Path) -> None:
    inputs = tmp_path / "Inputs"
    inputs.mkdir()
    (inputs / "S4-4诊断工作用表.xlsx").write_bytes(b"not-an-xlsx")
    _write_workbook(inputs / "S4-6评估总表.xlsx")

    manifest = build_manifest(tmp_path)

    assert [item.purpose for item in manifest.files] == ["s4-4", "s4-6"]


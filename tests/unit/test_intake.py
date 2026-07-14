import shutil
from pathlib import Path

from pds_report.domain.models import ParseStatus
from pds_report.infrastructure.intake import scan_project
from pds_report.infrastructure.project_store import ProjectStore

TEST_ROOT = Path(".test-projects/intake")


def fresh_project() -> Path:
    project_root = TEST_ROOT.resolve()
    if project_root.exists():
        shutil.rmtree(project_root)
    ProjectStore(project_root).create()
    return project_root


def test_scan_project_is_stable_and_relative() -> None:
    project_root = fresh_project()
    (project_root / "Inputs" / "巡检记录.md").write_text("温度：80°C", encoding="utf-8")
    (project_root / "Knowledge" / "方法.md").write_text("方法论", encoding="utf-8")

    first = scan_project(project_root)
    second = scan_project(project_root)

    assert [entry.id for entry in first.files] == [entry.id for entry in second.files]
    assert [entry.relative_path.as_posix() for entry in first.files] == [
        "Inputs/巡检记录.md",
        "Knowledge/方法.md",
    ]
    assert all(len(entry.sha256) == 64 for entry in first.files)


def test_scan_project_does_not_include_work_or_outputs() -> None:
    project_root = fresh_project()
    (project_root / "Inputs" / "input.txt").write_text("input", encoding="utf-8")
    (project_root / "Work" / "internal.json").write_text("{}", encoding="utf-8")
    (project_root / "Outputs" / "report.md").write_text("output", encoding="utf-8")

    manifest = scan_project(project_root)

    assert [entry.relative_path.as_posix() for entry in manifest.files] == [
        "Inputs/input.txt"
    ]


def test_scan_project_marks_unknown_format_unsupported() -> None:
    project_root = fresh_project()
    (project_root / "Inputs" / "raw.bin").write_bytes(b"\x00\x01")

    manifest = scan_project(project_root)

    assert manifest.files[0].format == "bin"
    assert manifest.files[0].parse_status is ParseStatus.UNSUPPORTED
    assert manifest.files[0].purposes == ["input"]

import json
import shutil
from pathlib import Path
from zipfile import Path as ZipPath
from zipfile import ZipFile

import pytest

from pds_report.app.service import ReportApplication
from pds_report.domain.models import RunStatus

PROJECT_ROOT = Path(".test-projects/phase-a").resolve()


def fresh_project() -> Path:
    if PROJECT_ROOT.exists():
        shutil.rmtree(PROJECT_ROOT)
    (PROJECT_ROOT / "Inputs").mkdir(parents=True)
    (PROJECT_ROOT / "Inputs" / "巡检记录.md").write_text(
        "主进线柜红外测温记录为 80°C。\n建议复核测点与环境温度。\n",
        encoding="utf-8",
    )
    return PROJECT_ROOT


@pytest.mark.asyncio
async def test_phase_a_flow_writes_traceable_project_artifacts() -> None:
    project_root = fresh_project()

    reply = await ReportApplication().run_message(
        project_root,
        "写作配电报告，要求深度思考，先做2.4",
    )

    assert reply.status is RunStatus.COMPLETED
    expected = [
        project_root / "Work" / "manifest.json",
        project_root / "Work" / "evidence.jsonl",
        project_root / "Work" / "coverage.json",
        project_root / "Outputs" / "Modules" / "2.4.json",
        project_root / "Outputs" / "Reviews" / f"{reply.run_id}.json",
        project_root / "Work" / "runs" / f"{reply.run_id}.json",
    ]
    assert all(path.is_file() for path in expected)

    evidence_rows = [
        json.loads(line)
        for line in (project_root / "Work" / "evidence.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    evidence_ids = {row["id"] for row in evidence_rows}
    module = json.loads(
        (project_root / "Outputs" / "Modules" / "2.4.json").read_text(encoding="utf-8")
    )

    assert evidence_ids
    assert module["claims"]
    assert all(set(claim["evidence_ids"]) <= evidence_ids for claim in module["claims"])
    assert reply.files
    assert "2.4" in reply.message


@pytest.mark.asyncio
async def test_knowledge_files_do_not_become_customer_evidence() -> None:
    project_root = fresh_project()
    (project_root / "Knowledge").mkdir(exist_ok=True)
    (project_root / "Knowledge" / "方法论.md").write_text(
        "知识库示例：温度超过 70°C 必然属于高风险。",
        encoding="utf-8",
    )

    await ReportApplication().run_message(project_root, "生成配电报告，先做2.4")

    evidence_text = (project_root / "Work" / "evidence.jsonl").read_text(encoding="utf-8")
    assert "知识库示例" not in evidence_text


@pytest.mark.asyncio
async def test_no_evidence_is_blocked_without_pending_permission() -> None:
    project_root = fresh_project()
    (project_root / "Inputs" / "巡检记录.md").unlink()

    reply = await ReportApplication().run_message(project_root, "生成配电报告，先做2.4")

    coverage = json.loads(
        (project_root / "Work" / "coverage.json").read_text(encoding="utf-8")
    )
    module = json.loads(
        (project_root / "Outputs" / "Modules" / "2.4.json").read_text(encoding="utf-8")
    )
    assert coverage["entries"][0]["status"] == "blocked"
    assert module["claims"][0]["pending_verification"] is True
    assert reply.status is RunStatus.COMPLETED
    assert "待核实" in reply.message


@pytest.mark.asyncio
async def test_application_loads_declarative_resources_from_zip(monkeypatch) -> None:
    project_root = fresh_project()
    archive_path = project_root / "resources.zip"
    source_root = Path("src/pds_report/resources")
    with ZipFile(archive_path, "w") as archive:
        for path in source_root.rglob("*"):
            if path.is_file():
                archive.write(path, f"pds_report/resources/{path.relative_to(source_root)}")

    with ZipFile(archive_path) as archive:
        resource_root = ZipPath(archive, "pds_report/resources/")
        monkeypatch.setattr("pds_report.app.service.resources.files", lambda package: resource_root)
        reply = await ReportApplication().run_message(
            project_root,
            "生成配电报告，先做2.4",
        )

    assert reply.status is RunStatus.COMPLETED

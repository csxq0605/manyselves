from __future__ import annotations

import threading
import time
from pathlib import Path
from zipfile import ZipFile

import pytest

from manyselves.capabilities.distribution_reporting.runtime.models.reporting import (
    EvidenceItem,
    ManifestFile,
    PhotoAsset,
    ProjectManifest,
    ReportRequest,
    SourceLocation,
)
from manyselves.core.loops.bus import MessageBus
from manyselves.core.providers.base import LLMProvider
from manyselves.core.reporting import preparation as preparation_module
from manyselves.core.reporting.mappers.common import MappingResult
from manyselves.core.reporting.preparation import (
    FilePreparationResult,
    prepare_manifest_file,
)
from manyselves.core.reporting.service import ReportingService
from manyselves.core.reporting.workflow import ReportWorkflowRunner
from manyselves.core.tools.task_board import TaskBoard


class NoCallProvider(LLMProvider):
    def __init__(self) -> None:
        super().__init__("test", model="no-call")

    async def chat(self, *args, **kwargs):
        raise AssertionError("preparation must not call the Provider")


def test_s4_6_worker_extracts_images_referenced_by_mapped_subtables(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    input_path = tmp_path / "Inputs/S4-6评估总表.xlsx"
    input_path.parent.mkdir()
    input_path.write_bytes(b"workbook")
    manifest_file = ManifestFile(
        id="file-s46",
        path=Path("Inputs/S4-6评估总表.xlsx"),
        sha256="a" * 64,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        purpose="s4-6",
    )
    evidence = EvidenceItem(
        id="provisional-s46",
        subject="结论建议",
        fact="现场照片显示异常",
        source=SourceLocation(
            file_id=manifest_file.id,
            path=manifest_file.path,
            sheet="结论建议汇总表",
            cell="G3",
        ),
        module_id="2.4",
        submodule_id="2.4.2.1",
        photo_refs=["ID_S46_PHOTO"],
    )
    observed: dict[str, object] = {}

    monkeypatch.setitem(
        preparation_module._MAPPERS,
        "s4-6",
        lambda *_args, **_kwargs: MappingResult(evidence_items=[evidence], gaps=[]),
    )

    def fake_extract(
        workbook_path: Path,
        *,
        output_dir: Path,
        required_image_ids: set[str] | None = None,
    ) -> dict[str, PhotoAsset]:
        observed.update(
            workbook_path=workbook_path,
            output_dir=output_dir,
            required_image_ids=required_image_ids,
        )
        return {
            "ID_S46_PHOTO": PhotoAsset(
                id="ID_S46_PHOTO",
                path=output_dir / "source-0001.jpeg",
                sha256="b" * 64,
                media_type="image/jpeg",
                source_member="xl/media/image1.jpeg",
                source_image_id="ID_S46_PHOTO",
            )
        }

    monkeypatch.setattr(
        "manyselves.core.reporting.preparation.extract_wps_images",
        fake_extract,
    )

    result = prepare_manifest_file(tmp_path, "run-s46", manifest_file, 2)

    assert result.status == "parsed"
    assert set(result.raw_photo_assets) == {"ID_S46_PHOTO"}
    assert observed["workbook_path"] == input_path
    assert observed["required_image_ids"] == {"ID_S46_PHOTO"}


@pytest.mark.asyncio
async def test_per_file_workers_overlap_but_reducer_keeps_manifest_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = ReportingService(
        tmp_path,
        bus=MessageBus(),
        task_board=TaskBoard(),
        llm_provider=NoCallProvider(),
    )
    files = [
        ManifestFile(
            id=f"file-{index}",
            path=Path(f"Inputs/{index}.txt"),
            sha256=f"{index}" * 64,
            media_type="text/plain",
        )
        for index in (1, 2, 3)
    ]
    active = 0
    maximum_active = 0
    lock = threading.Lock()

    def fake_prepare(
        _workspace: Path,
        _run_id: str,
        manifest_file: ManifestFile,
        order: int,
    ) -> FilePreparationResult:
        nonlocal active, maximum_active
        with lock:
            active += 1
            maximum_active = max(maximum_active, active)
        time.sleep(0.04 * (3 - order))
        with lock:
            active -= 1
        return FilePreparationResult(
            manifest_order=order,
            file_id=manifest_file.id,
            source_path=manifest_file.path,
            source_sha256=manifest_file.sha256,
            purpose=None,
            status="parsed",
            provisional_evidence=[
                EvidenceItem(
                    id=f"provisional-{manifest_file.id}",
                    subject=manifest_file.id,
                    fact=f"complete fact for {manifest_file.id}",
                    source=SourceLocation(
                        file_id=manifest_file.id,
                        path=manifest_file.path,
                    ),
                )
            ],
        )

    monkeypatch.setattr(
        "manyselves.core.reporting.service.prepare_manifest_file",
        fake_prepare,
    )
    state = {
        "run_id": "run-parallel-preparation",
        "request": ReportRequest(
            operation="module_report",
            instruction="prepare",
            target_modules=["2.1"],
            preparation_mode="deterministic_workers",
            preparation_concurrency=3,
        ),
        "project_manifest": ProjectManifest(files=files),
    }

    await service._parse_artifacts(state)
    await service._normalize_evidence(state)

    assert maximum_active == 3
    assert state["preparation_parallelism"]["reducer_order"] == [
        "file-1",
        "file-2",
        "file-3",
    ]
    assert [item.id for item in state["evidence_items"]] == [
        "E-0001",
        "E-0002",
        "E-0003",
    ]
    assert [item.subject for item in state["evidence_items"]] == [
        "file-1",
        "file-2",
        "file-3",
    ]


def test_same_run_recovery_projects_missing_s4_6_photo_from_frozen_workbook(
    tmp_path: Path,
) -> None:
    service = ReportingService(
        tmp_path,
        bus=MessageBus(),
        task_board=TaskBoard(),
        llm_provider=NoCallProvider(),
    )
    runner = object.__new__(ReportWorkflowRunner)
    runner.service = service
    run_id = "run-recover-s46-photo"
    snapshot_ref = Path(
        f"Work/runs/{run_id}/frozen-project/Inputs/S4-6评估总表.xlsx"
    )
    workbook_path = tmp_path / snapshot_ref
    workbook_path.parent.mkdir(parents=True)
    cell_images = b"""<?xml version="1.0" encoding="UTF-8"?>
    <etc:cellImages
      xmlns:etc="http://www.wps.cn/officeDocument/2017/etCustomData"
      xmlns:xdr="http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing"
      xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
      xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
      <etc:cellImage><xdr:pic><xdr:nvPicPr><xdr:cNvPr id="1" name="ID_S46"/></xdr:nvPicPr>
      <xdr:blipFill><a:blip r:embed="rId1"/></xdr:blipFill></xdr:pic></etc:cellImage>
      <etc:cellImage><xdr:pic><xdr:nvPicPr><xdr:cNvPr id="2" name="ID_DECORATION"/></xdr:nvPicPr>
      <xdr:blipFill><a:blip r:embed="rId2"/></xdr:blipFill></xdr:pic></etc:cellImage>
    </etc:cellImages>"""
    relationships = b"""<?xml version="1.0" encoding="UTF-8"?>
    <Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
      <Relationship Id="rId1" Type="image" Target="media/image1.jpeg"/>
      <Relationship Id="rId2" Type="image" Target="media/image2.jpeg"/>
    </Relationships>"""
    with ZipFile(workbook_path, "w") as archive:
        archive.writestr("xl/cellimages.xml", cell_images)
        archive.writestr("xl/_rels/cellimages.xml.rels", relationships)
        archive.writestr("xl/media/image1.jpeg", b"evidence-photo")
        archive.writestr("xl/media/image2.jpeg", b"decoration")

    manifest_file = ManifestFile(
        id="file-s46",
        path=Path("Inputs/S4-6评估总表.xlsx"),
        snapshot_ref=snapshot_ref,
        sha256="c" * 64,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        purpose="s4-6",
    )
    evidence = EvidenceItem(
        id="E-0001",
        subject="结论建议",
        fact="现场照片显示异常",
        source=SourceLocation(
            file_id=manifest_file.id,
            path=manifest_file.path,
            sheet="结论建议汇总表",
            cell="G3",
        ),
        module_id="2.4",
        submodule_id="2.4.2.1",
        photo_refs=["ID_S46"],
    )
    state = {
        "run_id": run_id,
        "project_manifest": ProjectManifest(files=[manifest_file]),
        "evidence_items": [evidence],
        "photo_assets": [],
    }

    runner._repair_runtime_photo_projection(state)

    assert [photo.id for photo in state["photo_assets"]] == ["ID_S46"]
    assert state["photo_assets"][0].primary_evidence_id == "E-0001"
    assert (tmp_path / state["photo_assets"][0].path).read_bytes() == b"evidence-photo"
    assert state["photo_evidence_adjacency"]["photo_to_evidence"] == {
        "ID_S46": ["E-0001"]
    }
    assert (
        tmp_path / f"Work/runs/{run_id}/recovery/photo-projection.json"
    ).is_file()

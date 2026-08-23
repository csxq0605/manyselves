"""Focused characterization for Capability-owned preparation tools."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from manyselves.capabilities.distribution_reporting.runtime.models.preparation import (
    FilePreparationResult,
    ManifestFile,
    PreparationContext,
    ProjectManifest,
)
from manyselves.capabilities.distribution_reporting.runtime.models.reporting import (
    EvidenceItem,
    ReportRequest,
    SourceLocation,
)
from manyselves.capabilities.distribution_reporting.runtime.storage import ReportingStore


def _context(run_id: str = "prep-tools") -> PreparationContext:
    return PreparationContext(
        run_id=run_id,
        request=ReportRequest(
            operation="module_report",
            instruction="characterize preparation",
            target_modules=["2.1"],
            preparation_mode="serial",
        ),
    )


def _tools(tmp_path: Path, snapshot: Any = None):
    from manyselves.capabilities.distribution_reporting.runtime.preparation_tools import (
        PreparationTools,
    )

    snapshot = snapshot or SimpleNamespace(inventory_digest="digest", files=[])
    photo_calls: list[tuple[list[Any], list[Any]]] = []

    def snapshot_content(source: Path, target: Path) -> tuple[Path, str, Path]:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(Path(source).read_bytes())
        return target, "unused", target

    def runtime_photo_ids(evidence: list[Any], photos: list[Any]) -> list[str]:
        photo_calls.append((evidence, photos))
        return [photo.id for photo in photos]

    tools = PreparationTools(
        workspace=tmp_path,
        input_snapshot=lambda _run_id: snapshot,
        snapshot_content=snapshot_content,
        runtime_photo_ids=runtime_photo_ids,
        store=ReportingStore(tmp_path),
    )
    return tools, photo_calls


def test_preparation_tools_do_not_depend_on_legacy_reporting_module() -> None:
    source = Path(
        "manyselves/capabilities/distribution_reporting/runtime/preparation_tools.py"
    ).read_text(encoding="utf-8")
    assert "manyselves.core.reporting" not in source


def test_preparation_implementation_bundle_binds_all_file_tool_ids(
    tmp_path: Path,
) -> None:
    from manyselves.capabilities.distribution_reporting.runtime.preparation_tools import (
        build_preparation_tool_implementations,
    )

    implementations = build_preparation_tool_implementations(
        workspace=tmp_path,
        input_snapshot=SimpleNamespace(inventory_digest="digest", files=[]),
        snapshot_content=lambda source, target: (target, "unused", target),
        runtime_photo_ids=lambda _evidence, _photos: [],
        store=ReportingStore(tmp_path),
    )

    assert set(implementations) == {
        "build-manifest",
        "prepare-report-taxonomy",
        "parse-artifacts",
        "normalize-evidence",
        "evaluate-coverage",
        "persist-preparation-snapshot",
        "restore-preparation-snapshot",
    }


def test_build_manifest_projects_the_frozen_input_snapshot_without_rehashing(
    tmp_path: Path,
) -> None:
    frozen = SimpleNamespace(
        logical_ref=Path("Inputs/report.txt"),
        snapshot_ref=Path(
            "Work/runs/prep-tools/frozen-project/Inputs/report.txt"
        ),
        sha256="a" * 64,
    )
    snapshot = SimpleNamespace(inventory_digest="inventory-1", files=[frozen])
    tools, _photo_calls = _tools(tmp_path, snapshot)

    result = tools.build_manifest(_context())

    assert result.input_snapshot_ref == "Work/runs/prep-tools/input-snapshot.json"
    assert result.input_snapshot_digest == "inventory-1"
    assert result.project_manifest is not None
    assert [item.path.as_posix() for item in result.project_manifest.files] == [
        "Inputs/report.txt"
    ]
    assert result.project_manifest.files[0].sha256 == "a" * 64
    assert result.project_manifest.files[0].snapshot_ref == frozen.snapshot_ref


def test_prepare_taxonomy_uses_the_manifest_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    from manyselves.capabilities.distribution_reporting.runtime import preparation_tools

    source = ManifestFile(
        id="file-s46",
        path=Path("Inputs/s4-6.xlsx"),
        snapshot_ref=Path("Work/runs/prep-tools/frozen-project/Inputs/s4-6.xlsx"),
        sha256="b" * 64,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        purpose="s4-6",
    )
    payload = {
        "schema_version": 1,
        "source_ref": source.snapshot_ref.as_posix(),
        "source_sha256": source.sha256,
        "sheet": "评估信息汇总表",
        "modules": [],
    }
    seen: dict[str, Any] = {}

    def parse(path: Path, *, source_ref: str, source_sha256: str) -> dict[str, Any]:
        seen.update(path=path, source_ref=source_ref, source_sha256=source_sha256)
        return payload

    monkeypatch.setattr(preparation_tools, "parse_report_taxonomy_workbook", parse)
    monkeypatch.setattr(preparation_tools, "activate_report_taxonomy", lambda _payload: None)
    tools, _photo_calls = _tools(Path("/tmp/preparation-tools-test"))
    context = _context().model_copy(
        update={"project_manifest": ProjectManifest(files=[source])}
    )

    result = tools.prepare_report_taxonomy(context)

    assert result.report_taxonomy == payload
    assert seen == {
        "path": tools.workspace
        / "Work/runs/prep-tools/frozen-project/Inputs/s4-6.xlsx",
        "source_ref": source.snapshot_ref.as_posix(),
        "source_sha256": source.sha256,
    }


@pytest.mark.asyncio
async def test_parse_artifacts_reduces_worker_results_in_manifest_order(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from manyselves.capabilities.distribution_reporting.runtime import preparation_tools

    first = ManifestFile(
        id="file-a",
        path=Path("Inputs/a.txt"),
        sha256="a" * 64,
        media_type="text/plain",
    )
    second = first.model_copy(update={"id": "file-b", "path": Path("Inputs/b.txt")})
    calls: list[str] = []

    def worker(
        _workspace: Path,
        _run_id: str,
        manifest_file: ManifestFile,
        manifest_order: int,
    ) -> FilePreparationResult:
        calls.append(manifest_file.id)
        return FilePreparationResult(
            manifest_order=manifest_order,
            file_id=manifest_file.id,
            source_path=manifest_file.path,
            source_sha256=manifest_file.sha256,
            status="parsed",
        )

    monkeypatch.setattr(preparation_tools, "prepare_manifest_file", worker)
    tools, _photo_calls = _tools(tmp_path)
    context = _context().model_copy(
        update={"project_manifest": ProjectManifest(files=[first, second])}
    )

    result = await tools.parse_artifacts(context)

    assert calls == ["file-a", "file-b"]
    assert [item.file_id for item in result.preparation_worker_results] == [
        "file-a",
        "file-b",
    ]
    assert result.preparation_parallelism == {
        "mode": "serial",
        "worker_count": 1,
        "file_count": 2,
        "reducer_order": ["file-a", "file-b"],
    }


def test_normalize_evidence_consumes_worker_results_only_and_writes_projection(
    tmp_path: Path,
) -> None:
    source = SourceLocation(file_id="file-a", path=Path("Inputs/a.txt"))
    evidence = EvidenceItem(
        id="ev-source",
        subject="设备",
        fact="存在可追溯观察",
        source=source,
        module_id="2.1",
        submodule_id="2.1.1",
    )
    worker = FilePreparationResult(
        manifest_order=0,
        file_id="file-a",
        source_path=Path("Inputs/a.txt"),
        source_sha256="a" * 64,
        status="parsed",
        provisional_evidence=[evidence],
        mapping_gaps=[{"code": "gap", "message": "待复核"}],
    )
    tools, photo_calls = _tools(tmp_path)
    context = _context().model_copy(
        update={
            "project_manifest": ProjectManifest(
                files=[
                    ManifestFile(
                        id="file-a",
                        path=Path("Inputs/a.txt"),
                        sha256="a" * 64,
                        media_type="text/plain",
                    )
                ]
            ),
            "preparation_worker_results": [worker],
        }
    )

    result = tools.normalize_evidence(context)

    assert [item.id for item in result.evidence_items] == ["E-0001"]
    assert result.mapping_gaps == [{"file_id": "file-a", "code": "gap", "message": "待复核"}]
    assert result.photo_evidence_adjacency["schema_version"] == 1
    assert photo_calls == [([result.evidence_items[0]], [])]
    assert (tmp_path / "Work/evidence.jsonl").is_file()
    assert (tmp_path / "Work/mapping-gaps.json").is_file()


def test_evaluate_coverage_uses_normalized_context_and_persists_result(
    tmp_path: Path,
) -> None:
    tools, _photo_calls = _tools(tmp_path)
    context = _context().model_copy(
        update={
            "evidence_items": [
                EvidenceItem(
                    id="E-0001",
                    subject="设备",
                    fact="存在可追溯观察",
                    source=SourceLocation(
                        file_id="file-a", path=Path("Inputs/a.txt")
                    ),
                    module_id="2.1",
                    submodule_id="2.1.1",
                )
            ]
        }
    )

    result = tools.evaluate_coverage(context)

    assert result.coverage_matrix is not None
    assert result.coverage_matrix.entries["2.1"].submodules["2.1.1"].evidence_ids == [
        "E-0001"
    ]
    assert (tmp_path / "Work/coverage.json").is_file()

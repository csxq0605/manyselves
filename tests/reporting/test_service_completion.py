import json
from pathlib import Path

import pytest
from docx import Document

from manyselves.core.loops.bus import MessageBus
from manyselves.core.providers.base import LLMProvider
from manyselves.core.reporting.models import OutputArtifact, ReportRequest
from manyselves.core.reporting.service import ReportingRunResult, ReportingService
from manyselves.core.reporting.workflow import ReportWorkflowRunner
from manyselves.core.tools.task_board import TaskBoard


class NeverProvider(LLMProvider):
    def __init__(self):
        super().__init__("fake", model="offline")
        self.calls = 0

    async def chat(self, *args, **kwargs):
        self.calls += 1
        raise AssertionError("delivery-only resume must not call Provider")


@pytest.mark.asyncio
async def test_completed_business_lifecycle_requires_readable_declared_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = ReportingService(
        tmp_path,
        bus=MessageBus(),
        task_board=TaskBoard(),
        llm_provider=NeverProvider(),
    )

    async def delivered(_runner, state: dict) -> None:
        state["delivery_status"] = "delivered"
        state["delivery_completion_ref"] = (
            f"Work/runs/{state['run_id']}/delivery-completion.json"
        )
        state["output_artifacts"] = [
            OutputArtifact(
                kind="report",
                path=Path("Outputs/Reports/declared-but-not-stat-checked.docx"),
            )
        ]

    monkeypatch.setattr(ReportWorkflowRunner, "run", delivered)

    result = await service.run(ReportRequest(instruction="offline lifecycle test"))

    assert result.status == "failed_before_delivery"
    assert result.output_paths == [
        tmp_path / "Outputs/Reports/declared-but-not-stat-checked.docx"
    ]
    assert not result.output_paths[0].exists()


@pytest.mark.asyncio
async def test_resume_legacy_archive_without_receipt_reenters_same_run_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = "report-legacy-archive-resume"
    provider = NeverProvider()
    service = ReportingService(
        tmp_path,
        bus=MessageBus(),
        task_board=TaskBoard(),
        llm_provider=provider,
    )
    service.store.write_json(
        f"Work/runs/{run_id}/request.json",
        ReportRequest(instruction="offline").model_dump(mode="json"),
    )
    service.store.write_json(
        f"Work/runs/{run_id}/workflow-state.json",
        {"run_id": run_id, "activity": "delivery", "status": "failed"},
    )
    service.store.write_json(
        f"Work/runs/{run_id}/delivery-completion.json",
        {
            "run_id": run_id,
            "status": "archive_failed",
            "delivery_status": "delivered_with_archive_warning",
            "warning": "legacy archive unavailable",
            "delivery_receipt_ref": None,
            "report_version_id": None,
            "output_artifacts": [
                {
                    "kind": "report",
                    "path": "Outputs/Reports/配电安全专家咨询报告.docx",
                    "module_id": None,
                }
            ],
        },
    )
    service.store.write_json(
        f"Work/runs/{run_id}.json",
        ReportingRunResult(
            run_id=run_id,
            status="delivered_with_archive_warning",
            error="legacy archive unavailable",
        ).model_dump(mode="json"),
    )

    resumed = False

    async def recover_same_run(*args, **kwargs):
        nonlocal resumed
        resumed = True
        return ReportingRunResult(
            run_id=run_id,
            status="failed_before_delivery",
            error="delivery must be regenerated",
        )

    monkeypatch.setattr(service, "_execute_locked", recover_same_run)
    result = await service.resume_run(run_id)

    assert result.status == "failed_before_delivery"
    assert resumed is True
    assert provider.calls == 0
    completion = json.loads(
        (tmp_path / f"Work/runs/{run_id}/delivery-completion.json").read_text()
    )
    assert completion["status"] == "archive_failed"


def test_republish_materialized_delivery_uses_current_run_files_without_hashes(
    tmp_path: Path,
) -> None:
    run_id = "report-materialized-republish"
    service = ReportingService(
        tmp_path,
        bus=MessageBus(),
        task_board=TaskBoard(),
        llm_provider=NeverProvider(),
    )
    delivery_dir = tmp_path / f"Work/runs/{run_id}/delivery/{run_id}-{run_id}"
    modules_dir = delivery_dir / "modules"
    modules_dir.mkdir(parents=True)
    final_docx = delivery_dir / "配电安全专家咨询报告.docx"
    source_index = delivery_dir / "证据与来源索引.md"
    source_index_docx = delivery_dir / "证据与来源索引.docx"
    report_state = delivery_dir / "report-state.json"
    manifest = delivery_dir / "delivery-manifest.json"
    document = Document()
    document.add_paragraph("new report bytes")
    document.save(final_docx)
    expected_docx = final_docx.read_bytes()
    source_index.write_text("new index", encoding="utf-8")
    source_index_docx.write_bytes(b"new index docx")
    report_state.write_text("{}\n", encoding="utf-8")
    manifest.write_text(
        json.dumps(
            {
                "status": "success",
                "modules": ["2.1", "2.2", "2.3", "2.4", "2.5"],
            }
        ),
        encoding="utf-8",
    )
    module_files = {}
    for module_id in ("2.1", "2.2", "2.3", "2.4", "2.5"):
        path = modules_dir / f"{module_id}.md"
        path.write_text(f"new module {module_id}", encoding="utf-8")
        module_files[module_id] = path
    service.store.write_json(
        f"Work/runs/{run_id}/delivery-receipt.json",
        {
            "success": True,
            "delivery_dir": delivery_dir.as_posix(),
            "final_docx": final_docx.as_posix(),
            "module_files": {
                module_id: path.as_posix()
                for module_id, path in module_files.items()
            },
            "report_state": report_state.as_posix(),
            "source_index": source_index.as_posix(),
            "source_index_docx": source_index_docx.as_posix(),
            "manifest_path": manifest.as_posix(),
        },
    )

    service._republish_materialized_delivery(run_id)

    assert (
        tmp_path / "Outputs/Reports/配电安全专家咨询报告.docx"
    ).read_bytes() == expected_docx
    assert (tmp_path / "Outputs/Modules/2.1.md").read_text(
        encoding="utf-8"
    ) == "new module 2.1"


def test_completed_run_finalization_does_not_require_output_owner(
    tmp_path: Path,
) -> None:
    run_id = "report-finalize-owner"
    service = ReportingService(
        tmp_path,
        bus=MessageBus(),
        task_board=TaskBoard(),
        llm_provider=NeverProvider(),
    )

    output = tmp_path / "Outputs/Reports/report.md"
    output.parent.mkdir(parents=True)
    output.write_bytes(b"report")
    result = service._finalize_completed_run(
        ReportingRunResult(run_id=run_id, status="completed", output_paths=[output]),
    )

    assert result.status == "completed"
    persisted = ReportingRunResult.model_validate_json(
        (tmp_path / f"Work/runs/{run_id}.json").read_text(encoding="utf-8")
    )
    assert persisted.status == "completed"
    assert persisted.output_paths == [output]


def test_completed_run_with_corrupt_docx_remains_resumable(
    tmp_path: Path,
) -> None:
    run_id = "report-finalize-corrupt-docx"
    service = ReportingService(
        tmp_path,
        bus=MessageBus(),
        task_board=TaskBoard(),
        llm_provider=NeverProvider(),
    )

    output = tmp_path / "Outputs/Reports/report.docx"
    output.parent.mkdir(parents=True)
    output.write_bytes(b"not-a-valid-docx-package")
    result = service._finalize_completed_run(
        ReportingRunResult(run_id=run_id, status="completed", output_paths=[output]),
    )

    assert result.status == "failed_before_delivery"
    assert "no readable declared outputs" in (result.error or "")
    persisted = ReportingRunResult.model_validate_json(
        (tmp_path / f"Work/runs/{run_id}.json").read_text(encoding="utf-8")
    )
    assert persisted.status == "failed_before_delivery"


def test_completed_run_finalization_ignores_unrelated_hash_cas_state(
    tmp_path: Path,
) -> None:
    run_id = "report-finalize-owner-failure"
    service = ReportingService(
        tmp_path,
        bus=MessageBus(),
        task_board=TaskBoard(),
        llm_provider=NeverProvider(),
    )

    service.store.write_json(
        "Work/output-owner.json",
        {"run_id": "another-run", "artifact_sha256": {"bad": "not-a-hash"}},
    )
    output = tmp_path / "Outputs/Reports/report.md"
    output.parent.mkdir(parents=True)
    output.write_bytes(b"report")
    result = service._finalize_completed_run(
        ReportingRunResult(run_id=run_id, status="completed", output_paths=[output]),
    )

    assert result.status == "completed"
    persisted = ReportingRunResult.model_validate_json(
        (tmp_path / f"Work/runs/{run_id}.json").read_text(encoding="utf-8")
    )
    assert persisted.status == "completed"

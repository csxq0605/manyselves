import json
from pathlib import Path

import pytest

from manyselves.core.loops.bus import MessageBus
from manyselves.core.providers.base import LLMProvider
from manyselves.core.reporting.delivery import ProjectDelivery
from manyselves.core.reporting.models import ReportRequest
from manyselves.core.reporting.service import ReportingRunResult, ReportingService
from manyselves.core.tools.task_board import TaskBoard

from test_delivery import _package


class NeverProvider(LLMProvider):
    def __init__(self):
        super().__init__("fake", model="offline")
        self.calls = 0

    async def chat(self, *args, **kwargs):
        self.calls += 1
        raise AssertionError("archive-only resume must not call Provider")


def _persist_delivery(workspace: Path, run_id: str) -> None:
    source_root = workspace / "inputs"
    source_root.mkdir(parents=True, exist_ok=True)
    package = _package(source_root).model_copy(update={"version": run_id})
    receipt = ProjectDelivery(
        workspace / f"Work/runs/{run_id}/delivery"
    ).deliver(package)
    receipt_ref = workspace / f"Work/runs/{run_id}/delivery-receipt.json"
    receipt_ref.parent.mkdir(parents=True, exist_ok=True)
    receipt_ref.write_text(receipt.model_dump_json(indent=2) + "\n", encoding="utf-8")
    artifacts = [
        {"kind": "report", "path": receipt.final_docx.relative_to(workspace).as_posix()}
    ]
    (workspace / f"Work/runs/{run_id}/delivery-completion.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "status": "archive_failed",
                "delivery_status": "delivered_with_archive_warning",
                "warning": "archive unavailable",
                "delivery_receipt_ref": receipt_ref.relative_to(workspace).as_posix(),
                "report_version_id": None,
                "output_artifacts": artifacts,
            }
        ),
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_resume_archive_only_restores_receipt_without_provider(tmp_path: Path) -> None:
    run_id = "run-archive-resume"
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
    _persist_delivery(tmp_path, run_id)
    service.store.write_json(
        f"Work/runs/{run_id}.json",
        ReportingRunResult(
            run_id=run_id,
            status="delivered_with_archive_warning",
            error="archive unavailable",
        ).model_dump(mode="json"),
    )

    result = await service.resume_run(run_id)

    assert result.status == "completed"
    assert provider.calls == 0
    completion = json.loads(
        (tmp_path / f"Work/runs/{run_id}/delivery-completion.json").read_text()
    )
    assert completion["status"] == "archived"

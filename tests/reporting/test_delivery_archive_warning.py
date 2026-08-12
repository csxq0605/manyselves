import json
from pathlib import Path

import pytest

from manyselves.core.loops.bus import MessageBus
from manyselves.core.reporting.models import ReportRequest
from manyselves.core.reporting.service import ReportingRunResult, ReportingService
from manyselves.core.tools.task_board import TaskBoard

from test_service_completion import NeverProvider, _persist_delivery


@pytest.mark.asyncio
async def test_archive_failure_is_warning_and_never_replays_provider(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_id = "run-archive-warning"
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

    def fail_archive(*args, **kwargs):
        raise OSError("archive backend offline")

    monkeypatch.setattr(service, "archive_delivery", fail_archive)

    result = await service.resume_run(run_id)

    assert result.status == "delivered_with_archive_warning"
    assert "archive backend offline" in (result.error or "")
    assert provider.calls == 0
    completion = json.loads(
        (tmp_path / f"Work/runs/{run_id}/delivery-completion.json").read_text()
    )
    assert completion["status"] == "archive_failed"
    assert completion["delivery_status"] == "delivered_with_archive_warning"

import json
from pathlib import Path

import pytest

from autoreport.core.loops.bus import MessageBus
from autoreport.core.providers.base import LLMProvider
from autoreport.core.reporting.models import ReportRequest
from autoreport.core.reporting.service import ReportingService
from autoreport.core.tools.reporting_tool import RunReportingWorkflowTool
from autoreport.core.tools.task_board import TaskBoard


def test_reporting_service_rejects_missing_provider(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="requires an LLM provider"):
        ReportingService(
            tmp_path,
            bus=MessageBus(),
            task_board=TaskBoard(),
            llm_provider=None,  # type: ignore[arg-type]
        )


def test_reporting_tool_rejects_missing_provider(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="requires an LLM provider"):
        RunReportingWorkflowTool(
            workspace=tmp_path,
            bus=MessageBus(),
            task_board=TaskBoard(),
            llm_provider=None,  # type: ignore[arg-type]
        )


@pytest.mark.asyncio
async def test_service_returns_blocked_before_calling_provider_when_evidence_requires_confirmation(
    tmp_path: Path,
) -> None:
    class NeverCalledProvider(LLMProvider):
        def __init__(self):
            super().__init__("test", model="never-called")

        async def chat(self, messages, tools=None, temperature=0.1, max_tokens=8192):
            raise AssertionError("blocked workflow must not call the provider")

    service = ReportingService(
        tmp_path,
        bus=MessageBus(),
        task_board=TaskBoard(),
        llm_provider=NeverCalledProvider(),
    )

    result = await service.run(
        ReportRequest(
            instruction="生成设备模块",
            target_modules=["2.4"],
            missing_evidence_policy="ask",
        )
    )

    assert result.status == "blocked"
    assert result.missing_evidence
    assert result.error is None
    workflow_state = json.loads(
        (tmp_path / f"Work/runs/{result.run_id}/workflow-state.json").read_text(
            encoding="utf-8"
        )
    )
    assert workflow_state["status"] == "blocked"

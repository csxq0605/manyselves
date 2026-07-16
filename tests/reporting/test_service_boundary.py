from pathlib import Path

import pytest

from autoreport.core.loops.bus import MessageBus
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

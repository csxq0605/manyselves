from pathlib import Path

import pytest
from openpyxl import Workbook

from autoreport.core.loops.bus import MessageBus
from autoreport.core.tools.reporting_tool import RunReportingWorkflowTool
from autoreport.core.tools.task_board import TaskBoard


@pytest.mark.asyncio
async def test_reporting_tool_runs_phase_a_in_current_workspace(tmp_path: Path) -> None:
    inputs = tmp_path / "Inputs"
    inputs.mkdir()
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["设备", "状态"])
    sheet.append(["变压器 A", "正常"])
    workbook.save(inputs / "设备台账.xlsx")
    tool = RunReportingWorkflowTool(
        workspace=tmp_path,
        bus=MessageBus(),
        task_board=TaskBoard(),
    )

    result = await tool(instruction="生成模块", target_modules=["2.1"])

    assert result["status"] == "completed"
    assert result["output_paths"] == [
        "Outputs/Modules/2.1.md",
        "Outputs/Reviews/phase-a.json",
    ]

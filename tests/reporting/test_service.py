import json
from pathlib import Path

import pytest
from openpyxl import Workbook

from autoreport.core.loops.bus import MessageBus
from autoreport.core.reporting.models import ReportRequest
from autoreport.core.reporting.service import ReportingService
from autoreport.core.tools.task_board import TaskBoard
from autoreport.interfaces.types import AgentType, TaskStatus


@pytest.mark.asyncio
async def test_phase_a_blocks_without_customer_evidence(tmp_path: Path) -> None:
    bus = MessageBus()
    board = TaskBoard()
    service = ReportingService(tmp_path, bus=bus, task_board=board)

    result = await service.run(
        ReportRequest(instruction="生成 2.4 模块", target_modules=["2.4"])
    )

    assert result.status == "blocked"
    assert result.missing_evidence == ["2.4"]
    coverage = json.loads((tmp_path / "Work" / "coverage.json").read_text(encoding="utf-8"))
    assert coverage["entries"]["2.4"]["status"] == "blocked"
    assert not (tmp_path / "Outputs" / "Modules" / "2.4.md").exists()
    assert any(task.status is TaskStatus.BLOCKED for task in board.get_todolist(AgentType.MAIN))


@pytest.mark.asyncio
async def test_phase_a_writes_traceable_module_output_from_workbook(tmp_path: Path) -> None:
    inputs = tmp_path / "Inputs"
    inputs.mkdir()
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "测温记录"
    sheet.append(["设备", "温度"])
    sheet.append(["1号进线柜", 80])
    workbook.save(inputs / "巡检.xlsx")

    bus = MessageBus()
    board = TaskBoard()
    service = ReportingService(tmp_path, bus=bus, task_board=board)
    result = await service.run(
        ReportRequest(instruction="生成 2.4 模块", target_modules=["2.4"])
    )

    assert result.status == "completed"
    assert result.phases == ["intake", "coverage", "module", "quality"]
    module_path = tmp_path / "Outputs" / "Modules" / "2.4.md"
    assert module_path in result.output_paths
    module_text = module_path.read_text(encoding="utf-8")
    assert "1号进线柜" in module_text
    assert "巡检.xlsx" in (tmp_path / "Work" / "evidence.jsonl").read_text(encoding="utf-8")
    assert bus._queue.qsize() >= 2
    assert all(
        task.status is TaskStatus.COMPLETED
        for task in board.get_todolist(AgentType.MAIN)
    )

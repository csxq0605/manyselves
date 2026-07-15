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

    result = await service.run(ReportRequest(instruction="生成 2.4 模块", target_modules=["2.4"]))

    assert result.status == "blocked"
    assert "2.4.1.1" in result.missing_evidence
    coverage = json.loads((tmp_path / "Work" / "coverage.json").read_text(encoding="utf-8"))
    assert coverage["entries"]["2.4"]["status"] == "pending"
    assert not (tmp_path / "Outputs" / "Modules" / "2.4.md").exists()
    assert any(task.status is TaskStatus.BLOCKED for task in board.get_todolist(AgentType.MAIN))


@pytest.mark.asyncio
async def test_phase_a_writes_traceable_module_output_from_core_workbook(tmp_path: Path) -> None:
    inputs = tmp_path / "Inputs"
    inputs.mkdir()
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "低配评估详情"
    sheet.append([None] * 26)
    sheet.append(["配电房", "低压柜", "运行电流(A)", "变压器容量(kVA)"] + [None] * 22)
    sheet.append(["车间配电房", "1A2", 2300, 2500] + [None] * 22)
    workbook.save(inputs / "S4-4诊断工作用表.xlsx")

    bus = MessageBus()
    board = TaskBoard()
    service = ReportingService(tmp_path, bus=bus, task_board=board)
    result = await service.run(
        ReportRequest(
            instruction="生成 2.4 模块",
            target_modules=["2.4"],
            missing_evidence_policy="skip",
        )
    )

    assert result.status == "completed"
    assert result.phases == ["intake", "coverage", "module", "quality"]
    module_path = tmp_path / "Outputs" / "Modules" / "2.4.md"
    assert module_path in result.output_paths
    report_path = tmp_path / "Outputs" / "Reports" / "配电安全专家咨询报告.docx"
    assert report_path in result.output_paths
    assert report_path.exists()
    module_text = module_path.read_text(encoding="utf-8")
    assert "车间配电房/1A2" in module_text
    assert "pds.module24.configuration@1.0.0" in module_text
    evidence_text = (tmp_path / "Work" / "evidence.jsonl").read_text(encoding="utf-8")
    assert "S4-4诊断工作用表.xlsx" in evidence_text
    evidence_rows = [json.loads(line) for line in evidence_text.splitlines()]
    assert evidence_rows[0]["submodule_id"] == "2.4.1.1"
    draft_state = json.loads(
        (tmp_path / "Work" / "drafts" / "2.4.json").read_text(encoding="utf-8")
    )
    assert draft_state["approved"] is True
    render_log = json.loads(
        (tmp_path / "Outputs" / "Reports" / "render-log.json").read_text(encoding="utf-8")
    )
    assert render_log["output_sha256"]
    assert bus._queue.qsize() >= 2
    assert all(task.status is TaskStatus.COMPLETED for task in board.get_todolist(AgentType.MAIN))

import json
import shutil
from pathlib import Path

import pytest

from autoreport.core.loops.bus import MessageBus
from autoreport.core.reporting.models import ReportRequest
from autoreport.core.reporting.service import ReportingService
from autoreport.core.tools.task_board import TaskBoard

REAL_INPUT_DIR = Path("/Users/zzymima0000/Documents/Codex/work/写作上传材料")
CORE_FILES = (
    "S2-1收资表.xlsx",
    "S4-4诊断工作用表.xlsx",
    "S4-6评估总表.xlsx",
)


@pytest.mark.skipif(
    not all((REAL_INPUT_DIR / filename).is_file() for filename in CORE_FILES),
    reason="local V2 handoff workbooks are not available",
)
@pytest.mark.asyncio
async def test_real_three_workbook_phase_a_24_generates_audited_docx(
    tmp_path: Path,
) -> None:
    inputs = tmp_path / "Inputs"
    inputs.mkdir()
    for filename in CORE_FILES:
        shutil.copy2(REAL_INPUT_DIR / filename, inputs / filename)

    service = ReportingService(tmp_path, bus=MessageBus(), task_board=TaskBoard())
    result = await service.run(
        ReportRequest(
            instruction="根据三张核心工作表生成 2.4 配电设备安全状态报告",
            target_modules=["2.4"],
            missing_evidence_policy="skip",
        )
    )

    assert result.status == "completed", result.error
    report_path = tmp_path / "Outputs" / "Reports" / "配电安全专家咨询报告.docx"
    assert report_path in result.output_paths
    assert report_path.stat().st_size > 1_000_000

    evidence_rows = [
        json.loads(line)
        for line in (tmp_path / "Work" / "evidence.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert len(evidence_rows) >= 100
    assert any(
        row["subject"] == "车间配电房/2A2"
        and row["submodule_id"] == "2.4.1.1"
        and abs(row["value"] - 96.992) < 0.001
        for row in evidence_rows
    )

    coverage = json.loads((tmp_path / "Work" / "coverage.json").read_text(encoding="utf-8"))
    assert coverage["entries"]["2.4"]["submodules"]["2.4.2.2"]["status"] == "ready"
    assert coverage["entries"]["2.4"]["submodules"]["2.4.3.1"]["status"] == "ready"

    module_text = (tmp_path / "Outputs" / "Modules" / "2.4.md").read_text(encoding="utf-8")
    assert "未达到100%" in module_text
    assert "当前已过载" not in module_text
    report_state = json.loads((tmp_path / "Work" / "report-state.json").read_text(encoding="utf-8"))
    assert report_state["module_drafts"][0]["approved"] is True
    assert not any(issue["severity"] == "blocking" for issue in report_state["review_issues"])

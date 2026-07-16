import json
from pathlib import Path

import pytest

from autoreport.core.loops.bus import MessageBus
from autoreport.core.reporting.research.reference_library import ReferenceLibrary
from autoreport.core.reporting.source_ledger import SourceLedger
from autoreport.core.tools.reporting_research_tools import (
    OpenProjectSourceTool,
    OpenReferenceTool,
    PublishResearchNoteTool,
    SearchProjectEvidenceTool,
    SearchReferenceLibraryTool,
)
from autoreport.interfaces.types import ResearchNotePublishedMessage


def _write_evidence(workspace: Path) -> None:
    path = workspace / "Work/evidence.jsonl"
    path.parent.mkdir(parents=True)
    item = {
        "id": "E-007",
        "subject": "低压柜连接点",
        "fact": "红外测温记录",
        "source": {
            "file_id": "F-1",
            "path": "Inputs/红外.xlsx",
            "sheet": "Sheet1",
            "cell": "B2",
        },
        "value": 86,
        "unit": "℃",
    }
    path.write_text(json.dumps(item, ensure_ascii=False) + "\n", encoding="utf-8")


@pytest.mark.asyncio
async def test_project_evidence_search_and_open_return_only_e_items(tmp_path: Path):
    _write_evidence(tmp_path)
    search = SearchProjectEvidenceTool(tmp_path)
    opener = OpenProjectSourceTool(tmp_path)

    hits = await search("连接点 86")
    opened = await opener("E-007")

    assert hits["hits"][0]["id"] == "E-007"
    assert opened["evidence"]["source"]["cell"] == "B2"
    with pytest.raises(ValueError, match="E-"):
        await opener("R-001")


@pytest.mark.asyncio
async def test_reference_search_and_open_share_stable_r_id(tmp_path: Path):
    root = tmp_path / "Knowledge/供应商/手册"
    root.mkdir(parents=True)
    (root / "guide.md").write_text("连接点温升与负荷相关", encoding="utf-8")
    ledger = SourceLedger(tmp_path, "run-1")
    library = ReferenceLibrary(tmp_path)

    search_result = await SearchReferenceLibraryTool(library, ledger)("连接点 温升")
    open_result = await OpenReferenceTool(library, ledger)(
        search_result["hits"][0]["locator"]
    )

    assert search_result["hits"][0]["source_id"] == open_result["source_id"] == "R-001"


@pytest.mark.asyncio
async def test_publish_research_note_persists_artifact_then_announces(tmp_path: Path):
    bus = MessageBus()
    received: list[ResearchNotePublishedMessage] = []
    bus.subscribe(ResearchNotePublishedMessage, received.append)
    tool = PublishResearchNoteTool(
        workspace=tmp_path,
        bus=bus,
        workflow_id="wf-1",
        run_id="run-1",
        task_id="task-1",
        agent_id="module-2.1-specialist",
    )

    result = await tool(
        question="单点故障如何传播？",
        synthesis="备用路径不足会扩大停电范围。",
        source_ids=["E-007"],
        applicability="适用于本项目现有拓扑证据范围。",
    )
    await bus._notify_subscribers(await bus._queue.get())

    assert Path(tmp_path / result["artifact_ref"]).exists()
    assert received[0].artifact_refs == [result["artifact_ref"]]

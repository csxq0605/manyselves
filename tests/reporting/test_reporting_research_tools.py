import json
from pathlib import Path

import pytest

from manyselves.capabilities.distribution_reporting.runtime.models.reporting import EvidenceItem
from manyselves.capabilities.distribution_reporting.runtime.research.evidence_memory import (
    EvidenceResearchMemory,
)
from manyselves.capabilities.distribution_reporting.runtime.research.reference_library import (
    ReferenceLibrary,
)
from manyselves.capabilities.distribution_reporting.runtime.research_tools import (
    OpenProjectSourceTool,
    OpenReferenceTool,
    PublishResearchNoteTool,
    SearchProjectEvidenceTool,
    SearchReferenceLibraryTool,
)
from manyselves.capabilities.distribution_reporting.runtime.source_ledger import SourceLedger
from manyselves.core.loops.bus import MessageBus
from manyselves.interfaces.types import ResearchNotePublishedMessage


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
async def test_project_evidence_search_reuses_prepared_ledger_locator(tmp_path: Path):
    _write_evidence(tmp_path)
    ledger = SourceLedger(tmp_path, "run-1")
    item = EvidenceItem.model_validate_json(
        (tmp_path / "Work/evidence.jsonl").read_text(encoding="utf-8")
    )
    ledger.register_project(
        "E-007",
        "低压柜连接点",
        "Inputs/红外.xlsx；工作表=Sheet1；单元格=B2",
        item.model_dump_json(),
    )

    result = await SearchProjectEvidenceTool(tmp_path, ledger)("连接点")

    assert result["hits"][0]["id"] == "E-007"
    assert ledger.records[0].locator == "Inputs/红外.xlsx；工作表=Sheet1；单元格=B2"


@pytest.mark.asyncio
async def test_project_evidence_search_bounds_large_model_requested_limit(tmp_path: Path):
    _write_evidence(tmp_path)

    result = await SearchProjectEvidenceTool(tmp_path)("连接点", limit=30)

    assert result["requested_limit"] == 30
    assert result["applied_limit"] == 12
    assert "open_project_source" in result["guidance"]


@pytest.mark.asyncio
async def test_project_evidence_tools_share_a_runtime_research_guard(tmp_path: Path):
    _write_evidence(tmp_path)
    remaining = 1

    def guard() -> None:
        nonlocal remaining
        if remaining == 0:
            raise RuntimeError("RESEARCH_PHASE_COMPLETE")
        remaining -= 1

    search = SearchProjectEvidenceTool(tmp_path, research_guard=guard)
    opener = OpenProjectSourceTool(tmp_path, research_guard=guard)

    assert (await search("连接点"))["hits"]
    with pytest.raises(RuntimeError, match="RESEARCH_PHASE_COMPLETE"):
        await opener("E-007")


@pytest.mark.asyncio
async def test_project_evidence_tools_persist_deduplicated_module_memory(tmp_path: Path):
    _write_evidence(tmp_path)
    memory = EvidenceResearchMemory(tmp_path, "run-1", "2.4")
    search = SearchProjectEvidenceTool(tmp_path, evidence_memory=memory)
    opener = OpenProjectSourceTool(tmp_path, evidence_memory=memory)

    first = await search("连接点")
    second = await search("连接点")
    opened = await opener("E-007")

    saved = json.loads((tmp_path / memory.relative_path).read_text(encoding="utf-8"))
    assert list(saved["evidence"]) == ["E-007"]
    assert saved["queries"] == [
        {"query": "连接点", "applied_limit": 10, "evidence_ids": ["E-007"]}
    ]
    assert first["cache_hit"] is False
    assert second["cache_hit"] is True
    assert opened["cache_hit"] is True
    assert saved["metrics"] == {
        "query_calls": 2,
        "cache_hits": 1,
        "index_searches": 1,
        "source_opens": 1,
        "source_cache_hits": 1,
    }


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
async def test_global_reference_tools_expose_namespace_and_ledger_metadata(tmp_path: Path):
    global_root = tmp_path / "global"
    global_root.mkdir()
    (global_root / "standard.md").write_text("shared protection standard", encoding="utf-8")
    ledger = SourceLedger(tmp_path, "run-1")
    library = ReferenceLibrary(tmp_path, global_root=global_root)

    search_result = await SearchReferenceLibraryTool(library, ledger)("protection standard")
    opened = await OpenReferenceTool(library, ledger)(
        search_result["hits"][0]["locator"]
    )

    assert search_result["hits"][0]["namespace"] == "global"
    assert opened["namespace"] == "global"
    assert opened["locator"] == "GlobalKnowledge/standard.md"
    assert ledger.records[0].scope_note == "knowledge_namespace=global"


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

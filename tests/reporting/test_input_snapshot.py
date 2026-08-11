from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from docx import Document

from manyselves.core.loops.bus import MessageBus
from manyselves.core.providers.base import LLMProvider
from manyselves.core.reporting.input_snapshot import RunInputSnapshotStore
from manyselves.core.reporting.models import ReportRequest
from manyselves.core.reporting.research.reference_library import ReferenceLibrary
from manyselves.core.reporting.service import ReportingService
from manyselves.core.tools.task_board import TaskBoard


class NoCallProvider(LLMProvider):
    def __init__(self) -> None:
        super().__init__("test", model="no-call")

    async def chat(self, *args, **kwargs):
        raise AssertionError("input snapshot tests do not call the Provider")


@pytest.mark.asyncio
async def test_queued_run_uses_frozen_inputs_knowledge_and_template(
    tmp_path: Path,
) -> None:
    input_file = tmp_path / "Inputs/site.txt"
    knowledge_file = tmp_path / "Knowledge/guide.md"
    template_file = tmp_path / "Templates/report_template.docx"
    input_file.parent.mkdir(parents=True)
    knowledge_file.parent.mkdir(parents=True)
    template_file.parent.mkdir(parents=True)
    input_file.write_text("ORIGINAL INPUT", encoding="utf-8")
    knowledge_file.write_text("ORIGINAL KNOWLEDGE", encoding="utf-8")
    original_template = Document()
    original_template.add_paragraph("ORIGINAL TEMPLATE")
    original_template.save(template_file)
    service = ReportingService(
        tmp_path,
        bus=MessageBus(),
        task_board=TaskBoard(),
        llm_provider=NoCallProvider(),
    )
    request = ReportRequest(
        operation="module_report",
        instruction="freeze",
        target_modules=["2.1"],
    )
    run_id = service.prepare_run(request)

    input_file.write_text("CHANGED INPUT", encoding="utf-8")
    (tmp_path / "Inputs/later.txt").write_text("LATE FILE", encoding="utf-8")
    knowledge_file.write_text("CHANGED KNOWLEDGE", encoding="utf-8")
    changed_template = Document()
    changed_template.add_paragraph("CHANGED TEMPLATE")
    changed_template.save(template_file)
    state = {"run_id": run_id, "request": request}

    await service._build_manifest(state)
    snapshot = RunInputSnapshotStore(tmp_path).load(run_id)
    frozen_knowledge = snapshot.scope_root(tmp_path, "Knowledge")
    library = ReferenceLibrary(
        tmp_path,
        knowledge_root=frozen_knowledge,
        index_root=tmp_path / f"Work/runs/{run_id}/indexes/knowledge",
    )
    selected_template, source = service.resolve_report_template(run_id)

    assert [item.path.as_posix() for item in state["project_manifest"].files] == [
        "Inputs/site.txt"
    ]
    manifest_file = state["project_manifest"].files[0]
    assert manifest_file.sha256 == hashlib.sha256(b"ORIGINAL INPUT").hexdigest()
    assert manifest_file.snapshot_ref is not None
    assert (tmp_path / manifest_file.snapshot_ref).read_text(encoding="utf-8") == (
        "ORIGINAL INPUT"
    )
    assert library.open("Knowledge/guide.md").text == "ORIGINAL KNOWLEDGE"
    assert source == "project"
    assert Document(selected_template).paragraphs[0].text == "ORIGINAL TEMPLATE"

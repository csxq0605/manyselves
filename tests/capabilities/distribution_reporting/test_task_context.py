"""Authoritative facts and persisted draft updates do not require model searches."""
import json
from pathlib import Path

import pytest

from manyselves.capabilities.distribution_reporting.runtime.collaboration_tools import (
    WriteResultPartTool,
)
from manyselves.capabilities.distribution_reporting.runtime.models.agentic import TaskEnvelope
from manyselves.capabilities.distribution_reporting.runtime.models.reporting import (
    EvidenceItem,
    SourceLocation,
)
from manyselves.capabilities.distribution_reporting.runtime.storage import ReportingStore
from manyselves.capabilities.distribution_reporting.runtime.task_context import (
    bind_task_context,
    replace_inline_fact_packet,
    task_fact_packet,
)
from manyselves.config.schema import AgentDefaults
from manyselves.runtime.loops.agent_loop import AgentLoop
from manyselves.runtime.loops.bus import MessageBus
from manyselves.runtime.providers.base import Message
from manyselves.runtime.tools.registry import ToolRegistry


def envelope(revision=0):
    return TaskEnvelope(task_id="author", run_id="synthetic", agent_id="author", revision=revision,
                        objective="audit", allowed_outputs=["module_submission"], target_submodule_ids=["2.4.3.1"])


def test_fact_scope_is_complete_including_unassigned_and_last_anomaly(tmp_path):
    records = [EvidenceItem(id=f"E-{i:04d}", subject=f"fixture-{i}", fact="11.3 A unresolved" if i == 80 else "0.1 A",
                           module_id="2.4", submodule_id="2.4.3.1",
                           source=SourceLocation(file_id="f", path=Path("Inputs/synthetic.xlsx"), cell=f"J{i}"))
               for i in range(1, 81)]
    records.append(records[0].model_copy(update={"id": "E-0081", "module_id": None, "submodule_id": None}))
    records.append(records[0].model_copy(update={"id": "E-0082", "submodule_id": "2.4.1.1"}))
    records.append(records[0].model_copy(update={"id": "E-0083", "module_id": "2.2", "submodule_id": None}))
    ReportingStore(tmp_path).write_jsonl("Work/runs/synthetic/preparation/evidence.jsonl",
                                        [item.model_dump(mode="json") for item in records])
    packet = task_fact_packet(tmp_path, envelope(), "2.4")
    assert packet is not None
    assert "记录总数：81" in packet
    assert "11.3 A unresolved" in packet and "J80" in packet
    assert "E-0081" in packet and "E-0082" not in packet and "E-0083" not in packet
    # A submodule projection must not advertise completeness of the whole module.
    scope_line = next(line for line in packet.splitlines() if line.startswith("检索范围："))
    assert json.loads(scope_line.removeprefix("检索范围：")) == {
        "module_id": "2.4", "submodule_ids": ["2.4.3.1"], "includes_unassigned": True,
    }
    assert "本模块及未分配模块的全部记录" not in packet
    assert "范围外对象需要定向查询" in packet
    inline = replace_inline_fact_packet('<project_facts delivery_mode="reference">open it</project_facts>\nKNOWLEDGE', packet)
    again = replace_inline_fact_packet(inline, packet)
    assert inline == again
    assert inline.count("11.3 A unresolved") == 1
    assert "KNOWLEDGE" in inline and "open it" not in inline


def test_missing_snapshot_does_not_replace_prepared_facts_with_empty_scope(tmp_path):
    assert task_fact_packet(tmp_path, envelope(), "2.4") is None


@pytest.mark.asyncio
async def test_draft_revision_rebind_and_restart_read_current_durable_parts(tmp_path):
    def new_loop():
        return AgentLoop(agent_type="author", workspace=tmp_path, tools=ToolRegistry(), bus=MessageBus(),
                         llm_provider=None, config=AgentDefaults())

    loop = new_loop()
    task = envelope()
    store = ReportingStore(tmp_path)
    writer = WriteResultPartTool(task.run_id, task.task_id, task.revision, store)
    await writer(part_id="2.4.3.1", content="DRAFT-OLD")
    projection = bind_task_context(loop, tmp_path, task, output_contract="module_submission",
                                   render_prompt=lambda: "E-0080 11.3 A; finding F-7 still unresolved")
    history = [Message(role="system", content="s"), Message(role="user", content=projection.initial_message())]
    await writer(part_id="2.4.3.1", content="DRAFT-NEW")
    result = await projection.rebuild(history, loop=loop)
    text = "\n".join(m.content for m in result.messages)
    assert text.count("DRAFT-NEW") == 1 and "DRAFT-OLD" not in text
    assert "F-7 still unresolved" in text
    fresh_loop = new_loop()
    restored = bind_task_context(fresh_loop, tmp_path, task, output_contract="module_submission",
                                render_prompt=lambda: "E-0080 11.3 A; finding F-7 still unresolved")
    assert (await restored.rebuild(history, loop=fresh_loop)).messages == result.messages
    revised = bind_task_context(loop, tmp_path, envelope(1), output_contract="module_revision_submission",
                               render_prompt=lambda: json.dumps({"current_draft": "ACCEPTED-R1", "pending": ["F-8"]}))
    history.append(Message(role="user", content=revised.initial_message()))
    revision = await revised.rebuild(history, loop=loop)
    text = "\n".join(m.content for m in revision.messages)
    assert "ACCEPTED-R1" in text and "F-8" in text
    assert "DRAFT-NEW" not in text and "F-7" not in text

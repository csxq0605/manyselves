"""Only transient, nonredundant history is summarized; stable inputs precede drafts."""
import json
from dataclasses import asdict
from unittest.mock import AsyncMock

import pytest

from manyselves.capabilities.distribution_reporting.runtime.collaboration_tools import (
    WriteResultPartTool,
)
from manyselves.capabilities.distribution_reporting.runtime.models.agentic import TaskEnvelope
from manyselves.capabilities.distribution_reporting.runtime.storage import ReportingStore
from manyselves.capabilities.distribution_reporting.runtime.task_context import bind_task_context
from manyselves.config.schema import AgentDefaults
from manyselves.runtime.loops.agent_loop import AgentLoop
from manyselves.runtime.loops.bus import MessageBus
from manyselves.runtime.providers.anthropic_provider import AnthropicProvider
from manyselves.runtime.providers.base import LLMToolCall, Message
from manyselves.runtime.tools.registry import ToolRegistry


def prepare(tmp_path):
    loop = AgentLoop(agent_type="author", workspace=tmp_path, tools=ToolRegistry(),
                     bus=MessageBus(), llm_provider=None, config=AgentDefaults(working_memory_tokens=4096))
    loop.persist_handoff_summary = False
    loop._request_model_handoff_summary = AsyncMock(return_value=dict(
        progress=["old write superseded"], decisions=[], constraints=[], remaining_work=[], critical_refs=[]))
    task = TaskEnvelope(run_id="synthetic", task_id="author", agent_id="author", objective="write")
    inputs = {"text": "Exact input E-0014: 11.3 A; unresolved finding F-7"}
    projection = bind_task_context(loop, tmp_path, task, output_contract="module_submission",
                                   render_prompt=lambda: inputs["text"])
    writer = WriteResultPartTool(task.run_id, task.task_id, 0, ReportingStore(tmp_path))
    history = [Message(role="system", content="instructions", cache_control=True),
               Message(role="user", content=projection.initial_message())]
    return loop, task, inputs, projection, writer, history


@pytest.mark.asyncio
async def test_exact_large_write_is_excluded_before_summary_but_reconsidered_on_change(tmp_path):
    loop, task, inputs, projection, writer, history = prepare(tmp_path)
    prose = "Exact persisted prose. " * 1500
    result = await writer(part_id="part-1", content=prose)
    assert result["persisted"] is True
    history.extend([
        Message(role="assistant", content="", tool_calls=[LLMToolCall(
            id="write", name="write_result_part", arguments={"part_id": "part-1", "content": prose})]),
        Message(role="tool", content=json.dumps(result), tool_call_id="write", is_tool_result=True),
        Message(role="user", content="Keep unresolved finding F-7."),
    ])
    original = [asdict(m) for m in history]
    for _ in range(2):
        rebuilt = await projection.rebuild(history, loop=loop)
        assert prose in "\n".join(m.content for m in rebuilt.messages)
    loop._request_model_handoff_summary.assert_not_awaited()
    assert [asdict(m) for m in history] == original

    # A saved write ceases to be redundant when its persisted content changes.
    await writer(part_id="part-1", content="Revised prose")
    changed = await projection.rebuild(history, loop=loop)
    loop._request_model_handoff_summary.assert_awaited_once()
    summary_source = loop._request_model_handoff_summary.await_args.args[0]
    assert any(m.tool_calls and m.tool_calls[0].arguments.get("content") == prose for m in summary_source)
    assert any(m.tool_call_id == "write" for m in summary_source)
    assert "Revised prose" in "\n".join(m.content for m in changed.messages)
    assert changed.messages[-1].content == "Keep unresolved finding F-7."
    assert (await projection.rebuild(history, loop=loop)).messages == changed.messages
    loop._request_model_handoff_summary.assert_awaited_once()

    # Reverting to the exact draft, including after rebind, needs no summary.
    await writer(part_id="part-1", content=prose)
    await projection.rebuild(history, loop=loop)
    restored = bind_task_context(loop, tmp_path, task, output_contract="module_submission",
                                render_prompt=lambda: inputs["text"])
    await restored.rebuild(history, loop=loop)
    loop._request_model_handoff_summary.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("native", [True, False])
async def test_wire_cache_boundary_precedes_changing_draft_without_losing_tool_pair(tmp_path, native):
    loop, task, inputs, projection, writer, history = prepare(tmp_path)
    history.extend([
        Message(role="assistant", content="", tool_calls=[LLMToolCall(id="read", name="read", arguments={"ref": "E-0014"})]),
        Message(role="tool", content="Observation at J14", tool_call_id="read", is_tool_result=True),
    ])
    provider = object.__new__(AnthropicProvider)
    provider._supports_cache = native
    requests = []
    for version in (1, 2):
        await writer(part_id="part-1", content=f"Draft version {version}")
        rebuilt = await projection.rebuild(history, loop=loop)
        system, wire = provider._convert_messages(rebuilt.messages)
        requests.append((system, wire))
        serialized = json.dumps(wire)
        assert serialized.count(inputs["text"]) == 1
        assert serialized.count(f"Draft version {version}") == 1
        assert wire[-2]["content"][0]["id"] == wire[-1]["content"][0]["tool_use_id"] == "read"
        if native:
            stable, dynamic = wire[0]["content"]
            assert stable["cache_control"] == {"type": "ephemeral"}
            assert inputs["text"] in stable["text"] and "Draft version" not in stable["text"]
            assert "cache_control" not in dynamic and f"Draft version {version}" in dynamic["text"]
        else:
            assert isinstance(wire[0]["content"], str)
            assert "cache_control" not in json.dumps((system, wire))
    if native:
        assert requests[0][1][0]["content"][0] == requests[1][1][0]["content"][0]
        assert requests[0][1][0]["content"][1] != requests[1][1][0]["content"][1]
    # Inputs are rendered anew, never frozen under an old cache identity.
    inputs["text"] = "Corrected input E-0014: 12.8 A; unresolved finding F-7"
    restored = bind_task_context(loop, tmp_path, task, output_contract="module_submission",
                                render_prompt=lambda: inputs["text"])
    rebuilt = await restored.rebuild(history, loop=loop)
    _, wire = provider._convert_messages(rebuilt.messages)
    assert inputs["text"] in json.dumps(wire) and "11.3 A" not in json.dumps(wire)

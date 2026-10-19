"""Compaction must buy room for useful work while retaining semantic summaries."""
import json
from unittest.mock import AsyncMock

import pytest

from manyselves.config.schema import AgentDefaults
from manyselves.runtime.loops.agent_loop import (
    AgentLoop,
    _compact_messages_for_working_memory,
    _estimate_tokens,
)
from manyselves.runtime.loops.bus import MessageBus
from manyselves.runtime.providers.base import LLMToolCall
from manyselves.runtime.providers.base import Message as LLMMessage
from manyselves.runtime.tools.registry import ToolRegistry


def exchange(index, chars=1600):
    ref = f"Work/result-{index}.json"
    return [
        LLMMessage(role="assistant", content="", tool_calls=[
            LLMToolCall(id=f"read-{index}", name="read", arguments={"ref": ref}),
        ]),
        LLMMessage(role="tool", tool_call_id=f"read-{index}", is_tool_result=True,
                   content=json.dumps({"ref": ref, "content": "x" * chars})),
    ]


def loop(tmp_path):
    result = AgentLoop(
        agent_type="test", workspace=tmp_path, tools=ToolRegistry(), bus=MessageBus(),
        config=AgentDefaults(working_memory_tokens=4096), llm_provider=AsyncMock(),
    )
    result._request_model_handoff_summary = AsyncMock(return_value={
        "progress": ["Read the earlier records"],
        "decisions": ["Use the measured value, not the nominal value"],
        "constraints": [], "remaining_work": ["Check the last record"], "critical_refs": [],
    })
    return result


@pytest.mark.asyncio
async def test_semantic_compaction_leaves_room_for_the_next_tool_exchange(tmp_path):
    agent = loop(tmp_path)
    messages = [LLMMessage(role="system", content="system"), LLMMessage(role="user", content="check all records")]
    for index in range(24):
        messages.extend(exchange(index))
    compacted = await agent._compact_working_memory_async(messages)
    assert _estimate_tokens(compacted) <= int(4096 * .6)
    assert "Use the measured value" in compacted[1].content
    assert compacted[-2:] == messages[-2:]
    await agent._compact_working_memory_async([*compacted, *exchange(24)])
    assert agent._request_model_handoff_summary.await_count == 1


def test_latest_task_boundary_does_not_overfill_the_retained_budget():
    task = LLMMessage(role="user", content="task " * 250)
    messages = [LLMMessage(role="system", content="system"), task]
    for index in range(12):
        messages.extend(exchange(index, chars=900))
    compacted = _compact_messages_for_working_memory(messages, target_tokens=1800)
    assert _estimate_tokens(compacted) <= 1800
    assert task in compacted
    assert compacted[-2:] == messages[-2:]


@pytest.mark.asyncio
async def test_large_model_summary_never_evicts_unsummarized_history(tmp_path):
    agent = loop(tmp_path)
    agent._request_model_handoff_summary.return_value["progress"] = [f"step-{i}:" + "p" * 250 for i in range(8)]
    messages = [LLMMessage(role="system", content="system"), LLMMessage(role="user", content="review")]
    for index in range(24):
        messages.extend(exchange(index))
    compacted = await agent._compact_working_memory_async(messages)
    # The soft target may be exceeded by the real summary. Do not hit it by
    # dropping extra messages the summary model never received.
    assert _estimate_tokens(compacted) < agent.config.working_memory_tokens
    summarized = {id(m) for m in agent._request_model_handoff_summary.await_args.args[0]}
    retained = {id(m) for m in compacted}
    assert all(id(m) in summarized or id(m) in retained for m in messages)
    assert "step-7:" in compacted[1].content


@pytest.mark.asyncio
async def test_no_summary_call_when_only_the_required_current_input_is_oversized(tmp_path):
    agent = loop(tmp_path)
    messages = [LLMMessage(role="system", content="system"),
                LLMMessage(role="user", content="current task " * 2000)]
    result = await agent._compact_working_memory_async(messages)
    assert result is messages
    agent._request_model_handoff_summary.assert_not_awaited()

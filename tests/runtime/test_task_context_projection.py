"""Task state is authoritative input, not an evictable memory of a tool read."""
import json
from unittest.mock import AsyncMock

import pytest

from manyselves.config.schema import AgentDefaults
from manyselves.runtime.loops.agent_loop import AgentLoop
from manyselves.runtime.loops.bus import MessageBus
from manyselves.runtime.loops.context_rebase import TaskContextRebuilder
from manyselves.runtime.providers.base import LLMStreamChunk, LLMToolCall, Message
from manyselves.runtime.tools.registry import ToolRegistry


def loop_at(path):
    loop = AgentLoop(agent_type="projection", workspace=path, tools=ToolRegistry(),
                     bus=MessageBus(), llm_provider=None, config=AgentDefaults(working_memory_tokens=4096))
    loop.persist_handoff_summary = False
    loop._request_model_handoff_summary = AsyncMock(return_value=dict(
        progress=["older work done"], decisions=[], constraints=[], remaining_work=[], critical_refs=[]))
    return loop


@pytest.mark.asyncio
async def test_current_state_survives_compaction_without_summary_rereads(tmp_path):
    loop = loop_at(tmp_path)
    state = {"reading": "11.3 A", "source": "E-0014", "unresolved": True}
    projection = TaskContextRebuilder("task-r1", lambda: json.dumps(state))
    messages = [Message(role="system", content="instructions"),
                Message(role="user", content="OLD TASK: 2.1 A all compliant"),
                Message(role="user", content=projection.initial_message())]
    for i in range(12):
        messages.extend([Message(role="assistant", content="", tool_calls=[LLMToolCall(
            id=str(i), name="read", arguments={"ref": str(i)})]),
            Message(role="tool", content="observation " * 300, is_tool_result=True, tool_call_id=str(i))])
    original = list(messages)
    first = await projection.rebuild(messages, loop=loop)
    text = "\n".join(m.content for m in first.messages)
    assert text.count("11.3 A") == 1
    assert "OLD TASK" not in text
    assert first.messages[-2:] == messages[-2:]
    assert messages == original
    calls = loop._request_model_handoff_summary.await_count
    assert calls == 1
    submitted = loop._request_model_handoff_summary.await_args.args[0]
    assert all("11.3 A" not in m.content for m in submitted)
    for _ in range(3):
        again = await projection.rebuild(messages, loop=loop)
        assert again.messages == first.messages
    assert loop._request_model_handoff_summary.await_count == calls
    state["reading"] = "12.8 A"
    revised = await projection.rebuild(messages, loop=loop)
    assert "12.8 A" in revised.messages[1].content
    assert "11.3 A" not in "\n".join(m.content for m in revised.messages)


@pytest.mark.asyncio
async def test_rebind_after_restart_excludes_old_task_but_keeps_correction(tmp_path):
    loop = loop_at(tmp_path)
    projection = TaskContextRebuilder("revision-2", lambda: "current draft; unresolved finding F-9")
    history = [Message(role="system", content="instructions"),
               Message(role="user", content="obsolete draft"),
               Message(role="user", content=projection.initial_message()),
               Message(role="user", content="schema correction: include evidence_ids")]
    rebuilt = await projection.rebuild(history, loop=loop)
    restored = TaskContextRebuilder("revision-2", lambda: "current draft; unresolved finding F-9")
    again = await restored.rebuild(history, loop=loop)
    assert again.messages == rebuilt.messages
    assert "obsolete draft" not in "\n".join(m.content for m in again.messages)
    assert again.messages[-1] == history[-1]


@pytest.mark.asyncio
async def test_restart_initial_capsule_keeps_same_task_completed_exchange(tmp_path):
    loop = loop_at(tmp_path)
    projection = TaskContextRebuilder("current", lambda: "fresh task state")
    other = TaskContextRebuilder("other", lambda: "unrelated task")
    exchange = [
        Message(role="assistant", content="", tool_calls=[LLMToolCall(
            id="done", name="read", arguments={"ref": "source"})]),
        Message(role="tool", content="completed source read", tool_call_id="done", is_tool_result=True),
    ]
    history = [Message(role="system", content="s"),
               Message(role="user", content=projection.initial_message()),
               Message(role="assistant", content="obsolete earlier task visit"),
               Message(role="user", content=other.initial_message()),
               Message(role="assistant", content="unrelated progress"),
               Message(role="user", content=projection.initial_message()),
               *exchange,
               Message(role="user", content=projection.initial_message())]
    rebuilt = await projection.rebuild(history, loop=loop)
    assert rebuilt.messages[2:] == exchange
    assert sum(m.content.count("fresh task state") for m in rebuilt.messages) == 1


@pytest.mark.asyncio
async def test_failed_summary_keeps_active_task_history(tmp_path):
    loop = loop_at(tmp_path)
    loop._request_model_handoff_summary.return_value = None
    projection = TaskContextRebuilder("task", lambda: "mandatory exact facts")
    messages = [Message(role="system", content="s"), Message(role="user", content=projection.initial_message())]
    messages.extend(Message(role="assistant", content=f"pending {i}:" + "x" * 4000) for i in range(6))
    result = await projection.rebuild(messages, loop=loop)
    assert result.messages[2:] == messages[2:]
    assert "mandatory exact facts" in result.messages[1].content


@pytest.mark.asyncio
async def test_real_loop_dispatch_does_not_rebase_summary_or_repeat_it(tmp_path):
    sent = []

    class Provider:
        model = "offline"
        context_window = 128000

        async def chat_stream(self, **kwargs):
            sent.append(kwargs["messages"])
            summary = kwargs["messages"][0].content.startswith("You are the context-compaction")
            body = json.dumps(dict(progress=["completed old operations"], decisions=[],
                                   constraints=[], remaining_work=[], critical_refs=[])) if summary else "next"
            yield LLMStreamChunk(delta=body, done=True, usage={"input_tokens": 100, "output_tokens": 10}, stop_reason="end_turn")

    loop = AgentLoop(agent_type="dispatch", workspace=tmp_path, bus=MessageBus(), tools=ToolRegistry(),
                     llm_provider=Provider(), config=AgentDefaults(working_memory_tokens=4096))
    projection = TaskContextRebuilder("current", lambda: "E-0014 measured 11.3 A; unresolved")
    loop.set_context_rebuilder(projection)
    history = [Message(role="system", content="s"), Message(role="user", content=projection.initial_message())]
    history.extend(Message(role="assistant", content="old progress " * 500) for _ in range(8))
    await loop._chat_with_retries(history, None, "one")
    await loop._chat_with_retries(history, None, "two")
    assert len(sent) == 3  # one summary, two ordinary requests
    assert "E-0014" not in "\n".join(m.content for m in sent[0])
    assert sent[1] == sent[2]
    assert sum(m.content.count("E-0014") for m in sent[1]) == 1
    assert loop._last_usage_record["phase"] == "provider"


@pytest.mark.asyncio
async def test_oversized_authoritative_state_is_not_silently_omitted(tmp_path):
    loop = loop_at(tmp_path)
    loop._context_window_for_request = lambda: 6000
    loop._chat_followup = AsyncMock()
    projection = TaskContextRebuilder("big", lambda: "MUST-KEEP " * 10000)
    loop.set_context_rebuilder(projection)
    response = await loop._chat_with_retries([
        Message(role="system", content="s"), Message(role="user", content=projection.initial_message())], None, "big")
    assert response.stop_reason == "context_budget_exhausted"
    loop._chat_followup.assert_not_awaited()


@pytest.mark.asyncio
async def test_large_fixed_state_does_not_trigger_history_summary_each_round(tmp_path):
    loop = loop_at(tmp_path)
    projection = TaskContextRebuilder("large-packet", lambda: "exact measurement " * 1500)
    messages = [Message(role="system", content="s"), Message(role="user", content=projection.initial_message())]
    for i in range(5):
        messages.append(Message(role="assistant", content=f"progress {i}"))
        result = await projection.rebuild(messages, loop=loop)
        assert result.messages[1].content.count("exact measurement") == 1500
    loop._request_model_handoff_summary.assert_not_awaited()

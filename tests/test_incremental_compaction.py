"""Incremental summaries cover removed history without rereading retained turns."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from manyselves.config.schema import AgentDefaults
from manyselves.runtime.loops.agent_loop import AgentLoop, _render_handoff_summary
from manyselves.runtime.loops.bus import MessageBus
from manyselves.runtime.providers.base import LLMResponse, LLMStreamChunk, LLMToolCall, Message
from manyselves.runtime.tools.registry import ToolRegistry

SUMMARY = dict(progress=["old work complete"], decisions=["keep exception"],
               constraints=[], remaining_work=["check next record"], critical_refs=[])


def make_loop(tmp_path):
    return AgentLoop(agent_type="test", workspace=tmp_path, tools=ToolRegistry(),
                     bus=MessageBus(), config=AgentDefaults(working_memory_tokens=4096),
                     llm_provider=SimpleNamespace(model="main-model"))


def history():
    messages = [Message(role="system", content="system"), Message(role="user", content="current task")]
    for i in range(24):
        messages.extend([
            Message(role="assistant", content="", tool_calls=[LLMToolCall(
                id=f"call-{i}", name="read", arguments={"ref": f"Work/{i}.txt"})]),
            Message(role="tool", tool_call_id=f"call-{i}", is_tool_result=True,
                    content=f"record-{i}:" + "x" * 1600),
        ])
    return messages


@pytest.mark.asyncio
async def test_summary_reads_exactly_removed_units_and_previous_summary(tmp_path):
    agent = make_loop(tmp_path)
    previous = Message(role="user", content=_render_handoff_summary(SUMMARY))
    messages = history()
    messages.insert(1, previous)
    agent._request_model_handoff_summary = AsyncMock(return_value=SUMMARY)
    compacted = await agent._compact_working_memory_async(messages)
    submitted = agent._request_model_handoff_summary.await_args.args[0]
    kept = {id(m) for m in compacted[2:]}
    expected = {id(m) for m in messages[1:] if id(m) not in kept}
    assert {id(m) for m in submitted} == expected
    assert previous in submitted
    assert messages[-2:] == compacted[-2:]
    assert all(m not in submitted for m in compacted[2:])


@pytest.mark.asyncio
async def test_previous_summary_is_not_retruncated_to_160_chars(tmp_path):
    agent = make_loop(tmp_path)
    decision = "a" * 200 + " PRESERVE-THIS-EXCEPTION"
    previous = Message(role="user", content=_render_handoff_summary({**SUMMARY, "decisions": [decision]}))
    agent._chat_with_retries = AsyncMock(return_value=LLMResponse(content=json.dumps(SUMMARY)))
    await agent._request_model_handoff_summary([previous, Message(role="user", content="new old work")])
    request = agent._chat_with_retries.await_args.args[0]
    assert decision in request[-1].content


@pytest.mark.asyncio
async def test_failed_summary_keeps_original_content(tmp_path):
    agent = make_loop(tmp_path)
    agent._request_model_handoff_summary = AsyncMock(return_value=None)
    messages = history()
    messages.insert(1, Message(role="user", content=_render_handoff_summary(SUMMARY)))
    compacted = await agent._compact_working_memory_async(messages)
    assert compacted is messages


@pytest.mark.asyncio
async def test_oversized_tool_arguments_do_not_silently_disappear(tmp_path):
    agent = make_loop(tmp_path)
    agent._context_window_for_request = lambda: 12000
    agent._chat_with_retries = AsyncMock(return_value=LLMResponse(content=json.dumps(SUMMARY)))
    messages = [Message(role="assistant", content="", tool_calls=[LLMToolCall(
        id="large", name="write", arguments={"text": "x" * 100000})]),
        Message(role="tool", content="done", is_tool_result=True, tool_call_id="large")]
    assert await agent._request_model_handoff_summary(messages) is None
    agent._chat_with_retries.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel", [False, True])
async def test_summary_model_is_request_scoped_and_restored(tmp_path, cancel):
    agent = make_loop(tmp_path)
    agent.config = AgentDefaults(context_compaction_model="claude-haiku-5-5")
    original = agent.llm_provider
    agent.provider_admission_model = "main-model"
    agent.usage_resolved_model = "main-model"

    async def request(*args, **kwargs):
        assert agent.llm_provider is not original
        assert agent.llm_provider.model == "claude-haiku-5-5"
        assert original.model == "main-model"
        assert agent.provider_admission_model == "claude-haiku-5-5"
        assert agent.usage_resolved_model == "claude-haiku-5-5"
        if cancel:
            raise asyncio.CancelledError()
        return LLMResponse(content=json.dumps(SUMMARY))

    agent._chat_with_retries = request
    if cancel:
        with pytest.raises(asyncio.CancelledError):
            await agent._request_model_handoff_summary([Message(role="user", content="old work")])
    else:
        assert await agent._request_model_handoff_summary([Message(role="user", content="old work")])
    assert agent.llm_provider is original
    assert agent.provider_admission_model == agent.usage_resolved_model == "main-model"


@pytest.mark.asyncio
async def test_summary_and_normal_requests_keep_distinct_model_usage(tmp_path):
    class Provider:
        model = "main-model"

        async def chat_stream(self, **kwargs):
            yield LLMStreamChunk(delta=json.dumps(SUMMARY), done=True,
                                 usage={"input_tokens": 100, "output_tokens": 20},
                                 stop_reason="end_turn")

    agent = make_loop(tmp_path)
    agent.config = AgentDefaults(context_compaction_model="claude-haiku-5-5")
    agent.llm_provider = Provider()
    await agent._request_model_handoff_summary([Message(role="user", content="old work")])
    assert agent._last_usage_record["model"] == "claude-haiku-5-5"
    assert agent._last_usage_record["resolved_model"] == "claude-haiku-5-5"
    assert agent._last_usage_record["phase"] == "context_compaction"
    await agent._chat_with_retries([Message(role="user", content="continue")], None, "normal")
    assert agent._last_usage_record["model"] == "main-model"
    assert agent._last_usage_record["resolved_model"] == "main-model"


@pytest.mark.asyncio
@pytest.mark.parametrize("stop", ["max_tokens", "refusal", "model_context_window_exceeded"])
async def test_incomplete_summary_cannot_authorize_eviction(tmp_path, stop):
    agent = make_loop(tmp_path)
    agent._chat_with_retries = AsyncMock(return_value=LLMResponse(
        content=json.dumps(SUMMARY), stop_reason=stop))
    messages = history()
    assert await agent._compact_working_memory_async(messages) is messages

"""Tests for agent loop processing engine."""

import asyncio
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from manyselves.config.schema import AgentDefaults
from manyselves.interfaces.types import (
    AgentResponse,
    AgentStatus,
    AgentType,
    QueueUpdateMessage,
    ReportMessage,
    SystemNotice,
    TaskUpdateMessage,
    ToolCallMessage,
    UserMessage,
)
from manyselves.interfaces.types import (
    ToolResult as ToolResultMsg,
)
from manyselves.runtime.loops import agent_loop as agent_loop_module
from manyselves.runtime.loops.agent_loop import (
    AGENT_MAX_TOKENS_CONTINUATION_REQUIRED,
    AgentLoop,
    _LoopLLMResponse,
)
from manyselves.runtime.loops.bus import MessageBus
from manyselves.runtime.providers.base import LLMResponse, LLMStreamChunk, LLMToolCall
from manyselves.runtime.providers.base import Message as LLMMessage
from manyselves.runtime.tools.registry import Tool, ToolRegistry
from manyselves.runtime.usage_ledger import UsageLedger


class _ParallelProbeTool(Tool):
    side_effect = "pure_read"
    parallel_safe = True

    def __init__(self, name: str, state: dict[str, int], delay: float) -> None:
        self.name = name
        self.state = state
        self.delay = delay

    async def __call__(self, value: int) -> dict:
        self.state["active"] += 1
        self.state["maximum"] = max(
            self.state["maximum"], self.state["active"]
        )
        try:
            await asyncio.sleep(self.delay)
            return {"value": value}
        finally:
            self.state["active"] -= 1


@pytest.fixture
def mock_gui():
    gui = AsyncMock()
    return gui


@pytest.fixture
def mock_provider():
    provider = AsyncMock()
    provider.model = "test-model"
    provider.chat.return_value = LLMResponse(
        content="I will help you.",
        tool_calls=[],
        usage={"input_tokens": 10, "output_tokens": 5},
    )

    # Add streaming support mock (async generator that yields chunks then done)
    async def mock_chat_stream(*args, **kwargs):
        from manyselves.runtime.providers.base import LLMStreamChunk
        yield LLMStreamChunk(delta="I will help you.")
        yield LLMStreamChunk(delta=None, done=True)

    provider.chat_stream = mock_chat_stream
    return provider


@pytest.fixture
def mock_prompt_loader():
    loader = MagicMock()
    loader.load_prompt.return_value = "You are a test agent.\n\n## Core Rules\n\nDo your job well."
    loader.load_shared_context.return_value = None  # No Common.md in test fixture
    return loader


@pytest.fixture
def config():
    return AgentDefaults(max_tool_iterations=5)


@pytest.fixture
def workspace():
    import shutil
    import tempfile
    ws = Path(tempfile.mkdtemp()).resolve()
    for d in ["Data", "Data/Processed", "Plots", "Plots/Fig", "Plots/Scripts", "Theory", "Tex", "References"]:
        (ws / d).mkdir()
    yield ws
    shutil.rmtree(ws, ignore_errors=True)


@pytest.fixture
def agent_loop(workspace, config, mock_gui, mock_provider, mock_prompt_loader):
    bus = MessageBus()
    tools = MagicMock()
    tools.get_definitions.return_value = []

    return AgentLoop(
        agent_type=AgentType.MAIN,
        workspace=workspace,
        tools=tools,
        bus=bus,
        config=config,
        llm_provider=mock_provider,
        prompt_loader=mock_prompt_loader,
        loop_manager=None,
    )


@pytest.mark.asyncio
async def test_parallel_safe_pure_read_batch_overlaps_and_preserves_result_order(
    agent_loop,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = {"active": 0, "maximum": 0}
    registry = ToolRegistry()
    registry.register(_ParallelProbeTool("pure_a", state, 0.05))
    registry.register(_ParallelProbeTool("pure_b", state, 0.05))
    agent_loop.tools = registry
    agent_loop._current_message = UserMessage(
        content="run pure tools",
        agent_type="main",
    )
    monkeypatch.setattr(
        agent_loop,
        "_chat_with_retries",
        AsyncMock(return_value=_LoopLLMResponse(content="done", tool_calls=[])),
    )
    response = _LoopLLMResponse(
        content="",
        tool_calls=[
            LLMToolCall(id="a", name="pure_a", arguments={"value": 1}),
            LLMToolCall(id="b", name="pure_b", arguments={"value": 2}),
        ],
    )
    started = asyncio.get_running_loop().time()

    await agent_loop._handle_tool_calls(response, "message")

    elapsed = asyncio.get_running_loop().time() - started
    assert state["maximum"] == 2
    assert elapsed < 0.09
    tool_results = [
        json.loads(message.content)
        for message in agent_loop._conversation_history
        if message.is_tool_result
    ]
    assert [result["value"] for result in tool_results] == [1, 2]


def test_agent_loop_keeps_dynamic_agent_id(
    workspace, config, mock_provider, mock_prompt_loader
):
    bus = MessageBus()
    tools = MagicMock()
    tools.get_definitions.return_value = []

    loop = AgentLoop(
        agent_type="evidence-auditor",
        workspace=workspace,
        tools=tools,
        bus=bus,
        config=config,
        llm_provider=mock_provider,
        prompt_loader=mock_prompt_loader,
    )

    assert loop.agent_id == "evidence-auditor"
    assert loop.agent_type == "evidence-auditor"


def test_init_subscribes_to_bus(agent_loop):
    """AgentLoop subscribes to UserMessage on init."""
    assert UserMessage in agent_loop.bus._subscribers


def test_status_initial(agent_loop):
    assert agent_loop.status == AgentStatus.IDLE


def test_debug_mode_default_off(agent_loop):
    assert agent_loop.debug_mode is False


def test_set_debug_mode_enabled(agent_loop):
    agent_loop.set_debug_mode(True)
    assert agent_loop.debug_mode is True
    # Should unsubscribe from bus
    assert len(agent_loop.bus._subscribers.get(UserMessage, [])) == 0


def test_set_debug_mode_disabled(agent_loop):
    agent_loop.set_debug_mode(True)
    agent_loop.set_debug_mode(False)
    assert agent_loop.debug_mode is False
    # Should re-subscribe to bus
    assert len(agent_loop.bus._subscribers.get(UserMessage, [])) == 1


def test_task_state_section_includes_task_id_for_respond(workspace, config, mock_provider, mock_prompt_loader):
    from manyselves.runtime.tools.task_board import TaskBoard

    board = TaskBoard()
    board.create_task(
        AgentType.MAIN,
        AgentType.PLOTTING,
        "draw final figure",
        task_id="tk123",
        session_id="main-session",
    )
    loop = _sub_loop(workspace, config, mock_provider, mock_prompt_loader, board)
    loop._current_session_id = "plotting-session"

    section = loop._build_task_state_section()

    assert "tk123" in section
    assert "draw final figure" in section


@pytest.mark.asyncio
async def test_system_prompt_first_call_loads_and_caches(agent_loop, mock_prompt_loader):
    prompt = await agent_loop._get_system_prompt()
    mock_prompt_loader.load_prompt.assert_called_once_with("main")
    assert "test agent" in prompt


@pytest.mark.asyncio
async def test_system_prompt_second_call_uses_cache(agent_loop, mock_prompt_loader):
    await agent_loop._get_system_prompt()  # first call — loads & caches
    prompt = await agent_loop._get_system_prompt()  # second call — cached
    # load_prompt should still only be called once
    assert mock_prompt_loader.load_prompt.call_count == 1
    assert "test agent" in prompt


@pytest.mark.asyncio
async def test_system_prompt_cached_across_calls(agent_loop, mock_prompt_loader):
    await agent_loop._get_system_prompt()  # first call
    await agent_loop._get_system_prompt()  # second call
    await agent_loop._get_system_prompt()  # third call
    assert mock_prompt_loader.load_prompt.call_count == 1  # Cached — not called again


@pytest.mark.asyncio
async def test_process_message(agent_loop, mock_provider, mock_gui):
    msg = UserMessage(content="Hello", agent_type=AgentType.MAIN)
    await agent_loop._process_message(msg)

    # Agent publishes AgentResponse to bus (not GUI call)
    assert agent_loop.status == AgentStatus.IDLE


@pytest.mark.asyncio
async def test_stream_final_thinking_snapshot_is_not_published_as_second_thought(agent_loop, mock_provider):
    thought = "I should answer directly."

    async def stream_with_final_thinking_snapshot(*args, **kwargs):
        yield LLMStreamChunk(thinking=thought)
        yield LLMStreamChunk(delta="Hi!")
        yield LLMStreamChunk(done=True, thinking=thought)

    mock_provider.chat_stream = stream_with_final_thinking_snapshot

    await agent_loop._process_message(UserMessage(content="hi", agent_type=AgentType.MAIN))

    published = [
        message
        for message in [
            *list(agent_loop.bus._queue._queue),
            *list(agent_loop.bus._stream_pending.values()),
        ]
        if isinstance(message, AgentResponse)
    ]
    thinking_messages = [m for m in published if m.thinking]
    final_messages = [m for m in published if not m.streaming and m.content == "Hi!"]

    assert [m.thinking for m in thinking_messages] == [thought]
    assert len(final_messages) == 1


@pytest.mark.asyncio
async def test_stream_final_thinking_snapshot_is_not_duplicated_in_response(
    agent_loop, mock_provider
):
    thought = "I should inspect the scoped regression."

    async def stream_with_final_thinking_snapshot(*args, **kwargs):
        yield LLMStreamChunk(thinking=thought)
        yield LLMStreamChunk(done=True, thinking=thought)

    mock_provider.chat_stream = stream_with_final_thinking_snapshot

    response = await agent_loop._chat_followup([], None, None)

    assert response.thinking == thought


@pytest.mark.asyncio
async def test_workflow_max_tokens_is_internal_continuation_not_normal_completion(
    agent_loop, mock_provider
):
    async def max_tokens_stream(*args, **kwargs):
        yield LLMStreamChunk(thinking="unfinished audit reasoning")
        yield LLMStreamChunk(
            done=True,
            usage={
                "input_tokens": 100,
                "output_tokens": 8192,
                "total_tokens": 8292,
            },
            stop_reason="max_tokens",
        )

    mock_provider.chat_stream = max_tokens_stream

    await agent_loop._process_message(
        UserMessage(
            content="audit module 2.4",
            agent_type=AgentType.MAIN,
            source="workflow",
            message_id="audit-2.4",
            internal=True,
        )
    )

    published = [
        message
        for message in list(agent_loop.bus._queue._queue)
        if isinstance(message, AgentResponse) and not message.streaming
    ]
    assert [message.content for message in published] == [
        AGENT_MAX_TOKENS_CONTINUATION_REQUIRED
    ]
    assert published[0].internal is True
    assert "task is not complete" in agent_loop._conversation_history[-1].content
    usage_rows = UsageLedger(agent_loop.workspace, "main").rows()
    assert usage_rows[-1]["usage_source"] == "provider"
    assert usage_rows[-1]["output_tokens"] == 8192


@pytest.mark.asyncio
async def test_process_message_includes_dispatch_summary_and_detail(agent_loop, mock_provider):
    captured_messages = []

    async def capture_chat_stream(*args, **kwargs):
        from manyselves.runtime.providers.base import LLMStreamChunk

        captured_messages.extend(kwargs["messages"])
        yield LLMStreamChunk(delta=None, done=True)

    mock_provider.chat_stream = capture_chat_stream

    msg = UserMessage(
        agent_type=AgentType.MAIN,
        source="main_agent",
        summary="Analyze the CSV",
        content="Use Data/raw.csv and report fit parameters.",
    )

    await agent_loop._process_message(msg)

    user_messages = [m for m in captured_messages if m.role == "user"]
    assert user_messages
    assert "[Task Summary]\nAnalyze the CSV" in user_messages[-1].content
    assert "[Task Detail]\nUse Data/raw.csv and report fit parameters." in user_messages[-1].content


@pytest.mark.asyncio
async def test_process_message_passes_message_id_to_pre_checkpoint(agent_loop):
    manager = MagicMock()
    manager.create_checkpoint = AsyncMock(return_value="cp_main_0001")
    agent_loop._loop_manager = manager

    msg = UserMessage(
        content="Hello",
        agent_type=AgentType.MAIN,
        message_id="msg-rollback-1",
    )
    await agent_loop._process_message(msg)

    manager.create_checkpoint.assert_awaited_once()
    assert manager.create_checkpoint.await_args.kwargs["message_id"] == "msg-rollback-1"


@pytest.mark.asyncio
async def test_process_message_with_tool_calls(agent_loop, mock_provider, mock_gui):
    tc = LLMToolCall(id="call_1", name="read", arguments={"path": "test.txt"})

    # Mock streaming: first yields chunks, then tool calls at end
    stream_calls = 0

    async def mock_chat_stream_with_tools(*args, **kwargs):
        nonlocal stream_calls
        from manyselves.runtime.providers.base import LLMStreamChunk
        stream_calls += 1
        if stream_calls == 1:
            yield LLMStreamChunk(delta="Reading file...")
            # At end, yield tool calls
            yield LLMStreamChunk(delta=None, tool_calls=[tc], done=True)
        else:
            yield LLMStreamChunk(delta="Done!")
            yield LLMStreamChunk(done=True)

    mock_provider.chat_stream = mock_chat_stream_with_tools

    tool_called = []
    async def mock_tool(**kwargs):
        tool_called.append(kwargs)
        return {"content": "file data"}

    agent_loop.tools.get = lambda name: mock_tool if name == "read" else None

    msg = UserMessage(content="Read file", agent_type=AgentType.MAIN)
    await agent_loop._process_message(msg)

    # Tool was executed with correct arguments
    assert len(tool_called) == 1
    assert tool_called[0] == {"path": "test.txt"}
    published = list(agent_loop.bus._queue._queue)
    calls = [item for item in published if isinstance(item, ToolCallMessage)]
    results = [item for item in published if isinstance(item, ToolResultMsg)]
    assert calls[0].tool_call_id == tc.id
    assert results[0].tool_call_id == calls[0].tool_call_id


@pytest.mark.asyncio
async def test_process_message_error(agent_loop, mock_provider, mock_gui):
    # Make chat_stream raise an error
    async def mock_chat_stream_error(*args, **kwargs):
        raise RuntimeError("API error")
        yield  # Never reached, but makes this an async generator

    mock_provider.chat_stream = mock_chat_stream_error

    msg = UserMessage(content="Hello", agent_type=AgentType.MAIN)
    await agent_loop._process_message(msg)

    assert agent_loop.status == AgentStatus.ERROR
    # Error is published to bus (not GUI call)


@pytest.mark.asyncio
async def test_handle_user_message_filters_by_agent_type(agent_loop):
    msg = UserMessage(content="Hello", agent_type=AgentType.THEORY)
    await agent_loop._handle_user_message(msg)
    assert agent_loop._message_queue.empty()


@pytest.mark.asyncio
async def test_handle_user_message_debug_mode_filters_main_agent(agent_loop):
    agent_loop.set_debug_mode(True)
    msg = UserMessage(
        content="Coordinate",
        agent_type=AgentType.MAIN,
        source="main_agent",
    )
    await agent_loop._handle_user_message(msg)
    assert agent_loop._message_queue.empty()


@pytest.mark.asyncio
async def test_handle_user_message_queues(agent_loop):
    msg = UserMessage(content="Hello", agent_type=AgentType.MAIN)
    await agent_loop._handle_user_message(msg)
    assert not agent_loop._message_queue.empty()


@pytest.mark.asyncio
async def test_handle_user_message_publishes_queue_update(agent_loop):
    msg = UserMessage(content="Hello", agent_type=AgentType.MAIN)
    await agent_loop._handle_user_message(msg)

    published = list(agent_loop.bus._queue._queue)
    assert any(isinstance(item, QueueUpdateMessage) for item in published)


@pytest.mark.asyncio
async def test_local_task_update_does_not_queue_llm_turn(agent_loop):
    msg = TaskUpdateMessage(
        task_id="tk001",
        action="completed",
        source_agent=AgentType.MAIN,
        target_agent=AgentType.MAIN,
        brief="local bookkeeping",
    )

    await agent_loop._handle_task_update(msg)

    assert agent_loop._message_queue.empty()


@pytest.mark.asyncio
async def test_delegated_task_update_does_not_queue_llm_turn(agent_loop):
    msg = TaskUpdateMessage(
        task_id="tk002",
        action="completed",
        source_agent=AgentType.MAIN,
        target_agent=AgentType.REPORT,
        brief="delegated report",
    )

    await agent_loop._handle_task_update(msg)

    assert agent_loop._message_queue.empty()
    published = list(agent_loop.bus._queue._queue)
    assert not any(isinstance(item, QueueUpdateMessage) for item in published)


@pytest.mark.asyncio
async def test_inter_agent_message_does_not_show_in_queue_preview(agent_loop):
    msg = UserMessage(
        content="Use Data/raw.csv",
        summary="Analyze raw CSV",
        agent_type=AgentType.MAIN,
        source="main_agent",
    )

    await agent_loop._handle_user_message(msg)

    assert not agent_loop._message_queue.empty()
    published = [item for item in agent_loop.bus._queue._queue if isinstance(item, QueueUpdateMessage)]
    assert published
    assert published[-1].queued_messages == []


def test_format_tool_result_dict(agent_loop):
    result = agent_loop._format_tool_result({"key": "value"})
    assert "key" in result


def test_format_tool_result_strips_ui_only_fields(agent_loop):
    result = agent_loop._format_tool_result({
        "status": "ok",
        "completion_summary": "visible in tool result",
        "_ui_summary": "ui only",
        "_ui_detail": "detail only",
    })
    assert "status" in result
    assert "completion_summary" not in result
    assert "_ui_summary" not in result
    assert "_ui_detail" not in result


def test_format_tool_result_keeps_inter_agent_summary(agent_loop):
    result = agent_loop._format_tool_result({
        "status": "success",
        "summary": "Short visible summary",
        "content": "Full response content",
    })
    assert "Short visible summary" in result
    assert "Full response content" in result


def test_format_tool_result_string(agent_loop):
    result = agent_loop._format_tool_result("plain text")
    assert result == "plain text"


@pytest.mark.asyncio
async def test_successful_batch_result_parts_preserve_required_content_in_provider_history(
    agent_loop,
):
    first_content = "第一部分正文。" * 80
    second_content = "第二部分正文。" * 80
    batch_tool = AsyncMock(
        return_value={
            "status": "completed",
            "parts": [
                {
                    "part_id": "part-a",
                    "artifact_ref": "Work/runs/run/result-parts/part-a.md",
                },
                {
                    "part_id": "part-b",
                    "artifact_ref": "Work/runs/run/result-parts/part-b.md",
                },
            ],
        }
    )
    agent_loop.tools.get = (
        lambda name: batch_tool if name == "write_result_parts" else None
    )
    agent_loop._chat_with_retries = AsyncMock(
        return_value=SimpleNamespace(
            content="批量正文已持久化。",
            tool_calls=[],
            thinking=None,
        )
    )
    response = SimpleNamespace(
        content="",
        thinking=None,
        tool_calls=[
            LLMToolCall(
                id="call-batch-result-parts",
                name="write_result_parts",
                arguments={
                    "parts": [
                        {"part_id": "part-a", "content": first_content},
                        {"part_id": "part-b", "content": second_content},
                    ]
                },
            )
        ],
        usage=None,
    )

    await agent_loop._handle_tool_calls(response, "msg-batch-result-parts")

    assistant_call = next(
        message
        for message in agent_loop._conversation_history
        if message.role == "assistant" and message.tool_calls
    ).tool_calls[0]
    retained_parts = assistant_call.arguments["parts"]
    assert "persisted_result_part" not in str(retained_parts)
    assert retained_parts == [
        {"part_id": "part-a", "content": first_content},
        {"part_id": "part-b", "content": second_content},
    ]


@pytest.mark.asyncio
async def test_failed_batch_result_parts_keep_original_content_for_provider_repair(
    agent_loop,
):
    first_content = "需要保留的第一部分错误上下文。" * 60
    second_content = "需要保留的第二部分错误上下文。" * 60
    batch_tool = AsyncMock(
        return_value={
            "status": "error",
            "error": "part-b violates the current result-part contract",
        }
    )
    agent_loop.tools.get = (
        lambda name: batch_tool if name == "write_result_parts" else None
    )
    agent_loop._chat_with_retries = AsyncMock(
        return_value=SimpleNamespace(
            content="我会根据错误修复批量参数。",
            tool_calls=[],
            thinking=None,
        )
    )
    response = SimpleNamespace(
        content="",
        thinking=None,
        tool_calls=[
            LLMToolCall(
                id="call-failed-batch-result-parts",
                name="write_result_parts",
                arguments={
                    "parts": [
                        {"part_id": "part-a", "content": first_content},
                        {"part_id": "part-b", "content": second_content},
                    ]
                },
            )
        ],
        usage=None,
    )

    await agent_loop._handle_tool_calls(response, "msg-failed-batch-result-parts")

    assistant_call = next(
        message
        for message in agent_loop._conversation_history
        if message.role == "assistant" and message.tool_calls
    ).tool_calls[0]
    assert assistant_call.arguments["parts"][0]["content"] == first_content
    assert assistant_call.arguments["parts"][1]["content"] == second_content
    assert any(
        "part-b violates the current result-part contract" in message.content
        for message in agent_loop._conversation_history
        if message.is_tool_result
    )


def test_large_tool_result_is_persisted_and_replaced_with_a_compact_reference(
    agent_loop, workspace
):
    payload = {"status": "ok", "content": "evidence-" + ("x" * 20000)}

    result = agent_loop._format_tool_result(
        payload,
        tool_name="search_project_evidence",
        tool_call_id="call-large",
    )

    compact = json.loads(result)
    assert len(result) <= agent_loop.config.max_tool_result_chars
    assert compact["full_result_ref"]
    assert compact["preview_offset"] == 0
    assert compact["preview_chars"] == compact["preview_end_offset"]
    assert compact["next_offset"] == compact["preview_end_offset"]
    assert compact["recommended_limit"] == 8000
    assert "省略 limit 即按 8000 字符读取" in compact["instruction"]
    stored = list((workspace / ".manyselves/artifacts/tool-result").glob("*.txt"))
    assert len(stored) == 1
    assert "evidence-" + ("x" * 20000) in stored[0].read_text(encoding="utf-8")
    assert not (workspace / ".manyselves/tool-results").exists()


def test_working_memory_compaction_persists_handoff_summary(agent_loop, workspace):
    messages = [
        LLMMessage(role="system", content="system"),
        LLMMessage(role="user", content="original task" + ("x" * 160000)),
        LLMMessage(role="user", content="recent task state"),
    ]

    compacted = agent_loop._compact_working_memory(messages)
    agent_loop._compact_working_memory(messages)

    assert len(compacted) < len(messages) + 1
    assert "<context_handoff_summary>" in compacted[1].content
    assert '"progress"' in compacted[1].content
    assert not (workspace / ".manyselves/artifacts/context-checkpoint").exists()
    assert not (workspace / ".manyselves/context-checkpoints").exists()


@pytest.mark.asyncio
async def test_working_memory_compaction_uses_model_handoff_when_available(agent_loop):
    agent_loop.config = AgentDefaults(max_tool_iterations=5, working_memory_tokens=5120)
    model_summary = {
        "progress": ["完成证据核验"],
        "decisions": ["保留当前提交"],
        "constraints": ["不得重写已完成分段"],
        "remaining_work": ["提交当前任务的 typed result"],
        "critical_refs": ["E-0001"],
    }
    agent_loop._chat_with_retries = AsyncMock(
        return_value=_LoopLLMResponse(
            content=json.dumps(model_summary, ensure_ascii=False),
            tool_calls=[],
        )
    )
    messages = [
        LLMMessage(role="system", content="system"),
        LLMMessage(role="user", content="task" + ("x" * 30000)),
        LLMMessage(role="user", content="continue"),
    ]

    compacted = await agent_loop._compact_working_memory_async(messages)

    assert agent_loop._chat_with_retries.await_args.kwargs["phase"] == "context_compaction"
    assert agent_loop._chat_with_retries.await_args.kwargs["reasoning_enabled"] is False
    assert '"source":"model"' in compacted[1].content
    assert "不得重写已完成分段" in compacted[1].content
    assert "checkpoint_ref" not in compacted[1].content


@pytest.mark.asyncio
async def test_model_handoff_disables_anthropic_reasoning_on_wire(agent_loop):
    from manyselves.runtime.providers.anthropic_provider import AnthropicProvider

    captured = {}
    model_summary = {
        "progress": ["bounded"],
        "decisions": [],
        "constraints": [],
        "remaining_work": ["continue"],
        "critical_refs": [],
    }

    class StreamContext:
        def __init__(self):
            self._events = [
                SimpleNamespace(
                    type="content_block_delta",
                    delta=SimpleNamespace(
                        type="text_delta",
                        text=json.dumps(model_summary),
                    ),
                )
            ]

        def __aiter__(self):
            return self

        async def __anext__(self):
            if not self._events:
                raise StopAsyncIteration
            return self._events.pop(0)

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        async def get_final_message(self):
            return SimpleNamespace(
                content=[
                    SimpleNamespace(
                        type="text",
                        text=json.dumps(model_summary),
                    )
                ],
                stop_reason="end_turn",
                usage=SimpleNamespace(
                    input_tokens=100,
                    cache_creation_input_tokens=0,
                    cache_read_input_tokens=0,
                    output_tokens=24,
                ),
            )

    class Messages:
        def stream(self, **kwargs):
            captured.update(kwargs)
            return StreamContext()

    provider = AnthropicProvider.__new__(AnthropicProvider)
    provider.model = "qwen3.8-flash"
    provider._supports_cache = False
    provider.client = SimpleNamespace(messages=Messages())
    agent_loop.llm_provider = provider

    summary = await agent_loop._request_model_handoff_summary(
        [LLMMessage(role="user", content="continue the active task")]
    )

    assert summary is not None
    assert captured["thinking"] == {"type": "disabled"}


@pytest.mark.asyncio
async def test_working_memory_compaction_marks_deterministic_fallback(agent_loop):
    agent_loop.config = AgentDefaults(max_tool_iterations=5, working_memory_tokens=5120)
    agent_loop._chat_with_retries = AsyncMock(side_effect=RuntimeError("provider unavailable"))
    messages = [
        LLMMessage(role="system", content="system"),
        LLMMessage(role="user", content="task" + ("x" * 30000)),
        LLMMessage(role="user", content="continue"),
    ]

    compacted = await agent_loop._compact_working_memory_async(messages)

    assert '"source":"deterministic_fallback"' in compacted[1].content
    assert "model_handoff_unavailable" in compacted[1].content


@pytest.mark.asyncio
async def test_model_handoff_input_is_bounded_for_long_history(agent_loop):
    agent_loop.config = AgentDefaults(
        max_tool_iterations=5,
        max_tokens=512,
        working_memory_tokens=4096,
    )
    agent_loop.llm_provider.context_window = 4096
    agent_loop._chat_with_retries = AsyncMock(
        return_value=_LoopLLMResponse(
            content=json.dumps(
                {
                    "progress": ["bounded"],
                    "decisions": [],
                    "constraints": [],
                    "remaining_work": ["continue"],
                    "critical_refs": [],
                }
            ),
            tool_calls=[],
        )
    )
    messages = [LLMMessage(role="system", content="system")]
    for index in range(20):
        messages.extend(
            [
                LLMMessage(role="user", content=f"old-{index}" + ("x" * 4000)),
                LLMMessage(
                    role="assistant",
                    content="",
                    tool_calls=[
                        LLMToolCall(
                            id=f"call-{index}",
                            name="read",
                            arguments={"ref": f"Work/{index}.json"},
                        )
                    ],
                ),
                LLMMessage(
                    role="tool",
                    content='{"status":"completed"}',
                    tool_call_id=f"call-{index}",
                    is_tool_result=True,
                ),
            ]
        )
    messages.append(LLMMessage(role="user", content="current task"))

    await agent_loop._compact_working_memory_async(messages)

    summary_request = agent_loop._chat_with_retries.await_args.args[0]
    assert len(summary_request[-1].content) < 20_000
    assert "<older_state_snapshot>" in summary_request[-1].content
    assert "<compaction_transcript>" in summary_request[-1].content


def test_identity_task_boundary_and_restart_restore_keep_lossless_history(agent_loop):
    assert agent_loop.begin_typed_task({"task_id": "task-1", "revision": 0}) is True
    agent_loop._conversation_history = [
        LLMMessage(role="user", content="task 1"),
        LLMMessage(
            role="assistant",
            content="",
            tool_calls=[
                LLMToolCall(id="call-old", name="submit_result", arguments={"payload": {"kind": "old"}})
            ],
        ),
    ]
    assert agent_loop.begin_typed_task({"task_id": "task-2", "revision": 1}) is False
    assert [message.content for message in agent_loop._conversation_history] == [
        "task 1",
        "",
    ]
    restored = AgentLoop(
        agent_type=AgentType.MAIN,
        workspace=agent_loop.workspace,
        tools=agent_loop.tools,
        bus=MessageBus(),
        config=agent_loop.config,
        llm_provider=agent_loop.llm_provider,
        loop_manager=None,
    )
    restored.restore_conversation(
        [
            {
                "role": "user",
                "content": "task 1",
            },
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"id": "call-old", "name": "submit_result", "arguments": {"payload": {"kind": "old"}}}
                ],
            },
        ],
        task_boundaries=agent_loop.task_boundaries,
        handoff_summary={"sequence": 2, "progress": ["saved"]},
    )
    assert restored.active_task_identity == {"task_id": "task-2", "revision": 1}
    assert restored._conversation_history[1].tool_calls[0].id == "call-old"
    assert restored.handoff_summary["progress"] == ["saved"]


def test_durable_handoff_summary_includes_history_after_last_compaction(agent_loop):
    agent_loop._handoff_summary = {
        "version": 1,
        "progress": ["older analysis"],
        "decisions": ["older decision"],
        "constraints": ["preserve evidence"],
        "remaining_work": ["stale remaining work"],
        "critical_refs": ["E-OLD"],
        "tool_state": [],
        "sequence": 3,
    }
    agent_loop._compaction_sequence = 3
    agent_loop._conversation_history = [
        LLMMessage(
            role="assistant",
            content="",
            tool_calls=[
                LLMToolCall(
                    id="call-new",
                    name="inspect_document",
                    arguments={"path": "Inputs/source.xlsx"},
                )
            ],
        ),
        LLMMessage(
            role="tool",
            content=json.dumps(
                {
                    "status": "blocked",
                    "next_action": "finish latest source analysis",
                    "result_path": "Work/runs/run-1/latest.json",
                }
            ),
            tool_call_id="call-new",
            is_tool_result=True,
        ),
    ]

    summary = agent_loop.durable_handoff_summary()

    assert "older decision" in summary["decisions"]
    assert "Inputs/source.xlsx" in summary["critical_refs"]
    assert summary["remaining_work"] == ["finish latest source analysis"]
    assert summary["tool_state"][-1]["tool_call_id"] == "call-new"
    assert summary["tool_state"][-1]["result_path"] == (
        "Work/runs/run-1/latest.json"
    )
    assert summary["sequence"] == 3


def test_token_usage_ledger_prefers_provider_usage(agent_loop, workspace):
    agent_loop.usage_run_id = "run-usage"
    agent_loop.usage_task_id = "task-usage"
    agent_loop.usage_context_manifest_ref = (
        "Work/runs/run-usage/context-manifests/provider-calls/"
        "task-usage-r0-session-c0001-initial-a1.json"
    )
    messages = [LLMMessage(role="user", content="x" * 10000)]
    response = SimpleNamespace(
        content="done",
        tool_calls=[],
        request_metrics={
            "representation": "test_provider_payload_v1",
            "request_fingerprint": "a" * 64,
            "message_fingerprint": "b" * 64,
            "tool_schema_fingerprint": "c" * 64,
            "request_chars": 777,
            "message_chars": 555,
            "tool_schema_chars": 111,
        },
        usage={
            "input_tokens": 123,
            "output_tokens": 17,
            "prompt_tokens_details": {"cached_tokens": 40},
            "cache_creation_input_tokens": 3,
        },
    )

    record = agent_loop._record_token_usage(
        messages,
        response,
        phase="initial",
        status="success",
        error=None,
        tool_definitions=[
            {
                "name": "submit_result",
                "input_schema": {"type": "object", "properties": {}},
            }
        ],
        duration_ms=25,
    )

    assert record["usage_source"] == "provider"
    assert record["input_tokens"] == 123
    assert record["output_tokens"] == 17
    assert record["cached_input_tokens"] == 40
    assert record["cache_write_input_tokens"] == 3
    assert record["uncached_input_tokens"] == 80
    assert (
        record["provider_call_id"]
        == "task-usage-r0-session-c0001-initial-a1"
    )
    assert record["duration_ms"] == 25
    assert record["request_metric_source"] == "provider_adapter_payload"
    assert record["provider_request_representation"] == "test_provider_payload_v1"
    assert record["request_fingerprint"] == "a" * 64
    assert record["message_fingerprint"] == "b" * 64
    assert record["tool_schema_fingerprint"] == "c" * 64
    assert record["request_chars"] == 777
    assert record["message_chars"] == 555
    assert record["tool_schema_chars"] == 111
    assert len(record["pre_adapter_request_fingerprint"]) == 64
    assert record["provider"] == agent_loop.llm_provider.__class__.__name__
    ledger = workspace / ".manyselves/usage/run-usage.jsonl"
    assert ledger.is_file()
    assert '"task_id": "task-usage"' in ledger.read_text(encoding="utf-8")


def test_token_usage_ledger_marks_length_fallback_as_estimated(agent_loop):
    response = SimpleNamespace(content="done", usage=None)

    record = agent_loop._record_token_usage(
        [LLMMessage(role="user", content="x" * 100)],
        response,
        phase="followup",
        status="success",
        error=None,
    )

    assert record["usage_source"] == "estimated"
    assert record["input_tokens"] > 0


def test_estimated_output_usage_counts_long_tool_call_arguments(agent_loop):
    long_content = "完整报告正文。" * 1000
    response = SimpleNamespace(
        content="",
        thinking=None,
        tool_calls=[
            LLMToolCall(
                id="write-long-batch",
                name="write_result_parts",
                arguments={
                    "parts": [
                        {"part_id": "part-a", "content": long_content}
                    ]
                },
            )
        ],
        usage=None,
        request_metrics=None,
    )

    record = agent_loop._record_token_usage(
        [LLMMessage(role="user", content="write the report")],
        response,
        phase="initial",
        status="success",
        error=None,
    )

    assert record["usage_source"] == "estimated"
    assert record["output_tokens"] >= int(len(long_content) * 0.25)
    assert record["total_tokens"] > record["input_tokens"]


def test_progress_monitor_requests_replan_after_repeated_identical_results():
    monitor = agent_loop_module._ProgressMonitor(replan_after=2)

    assert monitor.observe(["E-0001", "Work/a.json"]) is None
    assert monitor.observe(["E-0001", "Work/a.json"]) is None
    assert monitor.observe(["E-0001", "Work/a.json"]) == "replan"
    assert monitor.observe(["E-0002"]) is None


def test_get_agent_type_str(agent_loop):
    assert agent_loop._get_agent_type_str() == "main"


@pytest.mark.asyncio
async def test_start_stop(agent_loop):
    await agent_loop.start()
    assert agent_loop._running is True

    await agent_loop.stop()
    assert agent_loop._running is False


@pytest.mark.asyncio
async def test_stop_persists_latest_bounded_handoff(agent_loop, workspace):
    agent_loop.persist_handoff_summary = True
    agent_loop.usage_run_id = "run-stop-handoff"
    agent_loop._conversation_history = [
        LLMMessage(
            role="assistant",
            content="",
            tool_calls=[
                LLMToolCall(
                    id="call-stop",
                    name="inspect_document",
                    arguments={"path": "Inputs/latest.xlsx"},
                )
            ],
        )
    ]

    await agent_loop.start()
    await agent_loop.stop()

    handoff_path = (
        workspace
        / "Work"
        / "runs"
        / "run-stop-handoff"
        / "agent-conversations"
        / "main.handoff.json"
    )
    payload = json.loads(handoff_path.read_text(encoding="utf-8"))
    assert payload["summary"]["critical_refs"] == ["Inputs/latest.xlsx"]


@pytest.mark.asyncio
async def test_start_idempotent(agent_loop):
    await agent_loop.start()
    await agent_loop.start()
    assert agent_loop._running is True
    await agent_loop.stop()


@pytest.mark.asyncio
async def test_loop_marks_turn_reported_on_own_report(agent_loop):
    """A ReportMessage from this agent sets _turn_reported = True."""
    assert agent_loop._turn_reported is False
    await agent_loop.bus.publish(ReportMessage(
        agent_type=agent_loop.agent_type,
        task_id="tk1",
        report_type="reply",
        content="done",
    ))
    msg = await asyncio.wait_for(agent_loop.bus._queue.get(), timeout=1)
    await agent_loop.bus._notify_subscribers(msg)
    assert agent_loop._turn_reported is True


@pytest.mark.asyncio
async def test_cancel_during_tool_result_stops_before_next_llm_call(
    workspace, config, mock_provider, mock_prompt_loader
):
    bus = MessageBus()
    loop_ref = {}

    async def cancellable_tool(**kwargs):
        loop_ref["loop"].cancel_current()
        return {"status": "ok"}

    tools = MagicMock()
    tools.get.return_value = cancellable_tool
    tools.get_definitions.return_value = []
    loop = AgentLoop(
        agent_type=AgentType.PLOTTING,
        workspace=workspace,
        tools=tools,
        bus=bus,
        config=config,
        llm_provider=mock_provider,
        prompt_loader=mock_prompt_loader,
        loop_manager=None,
    )
    loop_ref["loop"] = loop
    response = SimpleNamespace(
        content="",
        thinking=None,
        tool_calls=[
            LLMToolCall(
                id="call_cancel",
                name="slow_tool",
                arguments={"path": "Plots/out.txt"},
            )
        ],
    )

    await loop._handle_tool_calls(response, "msg-1")

    mock_provider.chat.assert_not_called()


@pytest.mark.asyncio
async def test_tool_followup_streams_thinking_chunks(
    workspace, config, mock_provider, mock_prompt_loader
):
    bus = MessageBus()

    async def read_tool(**kwargs):
        return {"content": "file data"}

    async def followup_stream(*args, **kwargs):
        yield LLMStreamChunk(thinking="checking result")
        yield LLMStreamChunk(delta="Done")
        yield LLMStreamChunk(done=True)

    mock_provider.chat_stream = followup_stream
    tools = MagicMock()
    tools.get.return_value = read_tool
    tools.get_definitions.return_value = []
    loop = AgentLoop(
        agent_type=AgentType.MAIN,
        workspace=workspace,
        tools=tools,
        bus=bus,
        config=config,
        llm_provider=mock_provider,
        prompt_loader=mock_prompt_loader,
        loop_manager=None,
    )
    response = SimpleNamespace(
        content="",
        thinking=None,
        tool_calls=[
            LLMToolCall(
                id="call_read",
                name="read",
                arguments={"path": "test.txt"},
            )
        ],
    )

    await loop._handle_tool_calls(response, "msg-1")

    published = [
        *list(bus._queue._queue),
        *list(bus._stream_pending.values()),
    ]
    assert [
        msg.thinking
        for msg in published
        if isinstance(msg, AgentResponse) and msg.thinking
    ] == ["checking result"]


@pytest.mark.asyncio
async def test_successful_result_part_preserves_protocol_valid_followup_history(
    workspace, config, mock_provider, mock_prompt_loader
):
    full_content = "完整模块正文。" * 400
    observed_messages: list[list[LLMMessage]] = []

    async def write_part(**kwargs):
        assert kwargs["content"] == full_content
        return {
            "status": "created",
            "part_id": kwargs["part_id"],
            "characters": len(kwargs["content"]),
            "artifact_ref": "Work/runs/run-1/drafts/module-2.1/r0/2.1.1.md",
        }

    async def unsupported_stream(*args, **kwargs):
        raise NotImplementedError

    async def followup_chat(messages, **kwargs):
        observed_messages.append(messages)
        return LLMResponse(
            content="done",
            tool_calls=[],
            usage={"input_tokens": 20, "output_tokens": 2},
        )

    mock_provider.chat_stream = unsupported_stream
    mock_provider.chat = AsyncMock(side_effect=followup_chat)
    tools = MagicMock()
    tools.get.return_value = write_part
    tools.get_definitions.return_value = []
    loop = AgentLoop(
        agent_type=AgentType.THEORY,
        workspace=workspace,
        tools=tools,
        bus=MessageBus(),
        config=config,
        llm_provider=mock_provider,
        prompt_loader=mock_prompt_loader,
        loop_manager=None,
    )
    original_call = LLMToolCall(
        id="call-write-part",
        name="write_result_part",
        arguments={
            "part_id": "2.1.1",
            "content": full_content,
            "evidence_ids": [],
        },
    )

    await loop._handle_tool_calls(
        SimpleNamespace(
            content="",
            thinking=None,
            tool_calls=[original_call],
            usage=None,
        ),
        "msg-1",
    )

    assert observed_messages
    persisted_call = next(
        message.tool_calls[0]
        for message in observed_messages[0]
        if message.tool_calls
    )
    assert persisted_call.arguments["content"] == full_content
    assert "persisted_result_part" not in str(persisted_call.arguments)
    assert persisted_call.arguments["evidence_ids"] == []
    assert original_call.arguments["content"] == full_content


def test_working_memory_compaction_removes_complete_old_tool_exchange() -> None:
    old_content = "已经持久化的长正文。" * 4000
    old_call = LLMToolCall(
        id="call-old-write",
        name="write_result_part",
        arguments={"part_id": "part-a", "content": old_content},
    )
    messages = [
        LLMMessage(role="system", content="system"),
        LLMMessage(role="user", content="original task"),
        LLMMessage(role="assistant", content="", tool_calls=[old_call]),
        LLMMessage(
            role="user",
            content="{\"status\":\"created\"}",
            tool_call_id="call-old-write",
            is_tool_result=True,
        ),
        LLMMessage(role="assistant", content="old exchange complete"),
        LLMMessage(role="user", content="continue from current durable state"),
    ]

    compacted = agent_loop_module._compact_messages_for_working_memory(
        messages,
        target_tokens=300,
    )

    assert compacted[0].role == "system"
    assert "context_handoff_summary" in compacted[1].content
    assert all(
        call.id != "call-old-write"
        for message in compacted
        for call in (message.tool_calls or [])
    )
    assert all(message.tool_call_id != "call-old-write" for message in compacted)
    assert compacted[-1].content == "continue from current durable state"
    assert '"tool_state"' in compacted[1].content
    assert '"status":"created"' in compacted[1].content
    assert "persisted_result_part" not in compacted[1].content


def test_working_memory_compaction_keeps_complete_recent_tool_exchange() -> None:
    recent_call = LLMToolCall(
        id="call-recent-search",
        name="search_project_evidence",
        arguments={"query": "保护配合"},
    )
    messages = [
        LLMMessage(role="system", content="system"),
        LLMMessage(role="user", content="old context" + ("x" * 20_000)),
        LLMMessage(role="assistant", content="", tool_calls=[recent_call]),
        LLMMessage(
            role="user",
            content=json.dumps(
                {
                    "status": "ok",
                    "next_action": "write_result_part",
                    "evidence_refs": ["E-0001"],
                }
            ),
            tool_call_id="call-recent-search",
            is_tool_result=True,
        ),
        LLMMessage(role="assistant", content="已获得所需证据。"),
    ]

    compacted = agent_loop_module._compact_messages_for_working_memory(
        messages,
        target_tokens=1500,
    )

    retained_call_ids = {
        call.id for message in compacted for call in (message.tool_calls or [])
    }
    retained_result_ids = {
        message.tool_call_id for message in compacted if message.is_tool_result
    }
    assert retained_call_ids == {"call-recent-search"}
    assert retained_result_ids == {"call-recent-search"}
    assert '"critical_refs":["E-0001"]' in compacted[1].content


def test_working_memory_compaction_drops_adjacent_orphan_tool_result() -> None:
    messages = [
        LLMMessage(role="system", content="system"),
        LLMMessage(role="user", content="old context" + ("x" * 20_000)),
        LLMMessage(
            role="assistant",
            content="",
            tool_calls=[
                LLMToolCall(
                    id="call-valid",
                    name="search_project_evidence",
                    arguments={"query": "保护配合"},
                )
            ],
        ),
        LLMMessage(
            role="user",
            content='{"status":"ok"}',
            tool_call_id="call-valid",
            is_tool_result=True,
        ),
        LLMMessage(
            role="user",
            content='{"status":"orphan"}',
            tool_call_id="call-orphan",
            is_tool_result=True,
        ),
    ]

    compacted = agent_loop_module._compact_messages_for_working_memory(
        messages,
        target_tokens=1500,
    )

    retained_result_ids = {
        message.tool_call_id for message in compacted if message.is_tool_result
    }
    assert retained_result_ids == {"call-valid"}


@pytest.mark.parametrize(
    "call_ids",
    [
        ["", "call-valid"],
        ["call-duplicate", "call-duplicate"],
    ],
)
def test_atomic_history_rejects_empty_or_duplicate_assistant_call_ids(
    call_ids: list[str],
) -> None:
    history = [
        LLMMessage(
            role="assistant",
            content="",
            tool_calls=[
                LLMToolCall(id=call_id, name="calculate", arguments={"expression": "1"})
                for call_id in call_ids
            ],
        ),
        LLMMessage(
            role="user",
            content='{"status":"ok"}',
            tool_call_id=call_ids[-1],
            is_tool_result=True,
        ),
    ]

    assert agent_loop_module._atomic_history_units(history) == []


def test_repeated_working_memory_compaction_preserves_one_original_objective() -> None:
    objective = "分析五模块并保留 Cross、Chief 与 Final 的语义门禁。"
    first = agent_loop_module._compact_messages_for_working_memory(
        [
            LLMMessage(role="system", content="system"),
            LLMMessage(role="user", content=objective),
            LLMMessage(role="assistant", content="旧分析" + ("x" * 20_000)),
        ],
        target_tokens=700,
    )
    second = agent_loop_module._compact_messages_for_working_memory(
        [
            *first,
            LLMMessage(role="user", content="新增证据" + ("y" * 20_000)),
            LLMMessage(role="assistant", content="继续综合"),
        ],
        target_tokens=700,
    )

    provider_text = "\n".join(message.content or "" for message in second)
    assert provider_text.count("<context_handoff_summary>") == 1
    assert objective in provider_text
    assert "checkpoint_ref" not in provider_text


@pytest.mark.asyncio
async def test_provider_payload_never_exposes_retired_history_token(
    agent_loop,
) -> None:
    marker = (
        "<persisted_result_part part_id=part-a "
        f"sha256={'a' * 64} characters=100>"
    )
    retired_name = "persisted_result_part"
    messages = [
        LLMMessage(role="system", content=f"system {marker}"),
        LLMMessage(
            role="assistant",
            content="",
            tool_calls=[
                LLMToolCall(
                    id=f"old-call-{retired_name}",
                    name=f"write_{retired_name}",
                    arguments={retired_name: marker, "part_id": "part-a"},
                )
            ],
        ),
        LLMMessage(
            role="user",
            content=f"tool result mentioned {marker}",
            tool_call_id=f"old-call-{retired_name}",
            is_tool_result=True,
        ),
    ]
    tools = [
        {
            "name": "write_result_part",
            "description": f"legacy description {marker}",
            "input_schema": {
                "type": "object",
                "properties": {retired_name: {"type": "string"}},
            },
        }
    ]
    agent_loop._chat_followup = AsyncMock(
        return_value=_LoopLLMResponse(content="ok", tool_calls=[])
    )

    await agent_loop._chat_with_retries(messages, tools, "message-1")

    provider_messages = agent_loop._chat_followup.await_args.args[0]
    provider_tools = agent_loop._chat_followup.await_args.args[1]
    canonical_payload = json.dumps(
        {
            "messages": [
                {
                    "content": message.content,
                    "thinking": message.thinking,
                    "tool_calls": [
                        {
                            "name": call.name,
                            "arguments": call.arguments,
                        }
                        for call in (message.tool_calls or [])
                    ],
                }
                for message in provider_messages
            ],
            "tools": provider_tools,
        },
        ensure_ascii=False,
    )
    assert "persisted_result_part" not in canonical_payload.casefold()
    assert "persisted_result_part" in messages[0].content


@pytest.mark.asyncio
async def test_provider_followup_receives_repair_without_rejected_submit_call(
    agent_loop,
) -> None:
    malformed_payload = '{"kind":"cross_owner_finding_submission"'
    messages = [
        LLMMessage(role="system", content="system"),
        LLMMessage(role="user", content="review owner 2.3"),
        LLMMessage(
            role="assistant",
            content="analysis complete",
            tool_calls=[
                LLMToolCall(
                    id="call-rejected-submit",
                    name="submit_result",
                    arguments={"payload": malformed_payload},
                )
            ],
        ),
        LLMMessage(
            role="user",
            content=json.dumps(
                {
                    "status": "correction_required",
                    "accepted": False,
                    "repair_instruction": "Pass payload as a native object.",
                }
            ),
            tool_call_id="call-rejected-submit",
            is_tool_result=True,
        ),
    ]
    agent_loop._chat_followup = AsyncMock(
        return_value=_LoopLLMResponse(content="ok", tool_calls=[])
    )

    await agent_loop._chat_with_retries(messages, [], "message-1")

    provider_messages = agent_loop._chat_followup.await_args.args[0]
    assert not any(
        call.id == "call-rejected-submit"
        for message in provider_messages
        for call in (message.tool_calls or [])
    )
    assert not any(
        message.tool_call_id == "call-rejected-submit"
        for message in provider_messages
    )
    assert any(
        message.role == "user"
        and "Pass payload as a native object" in message.content
        for message in provider_messages
    )
    assert messages[2].tool_calls[0].arguments["payload"] == malformed_payload


def test_legacy_result_part_marker_restores_exact_same_task_disk_prose(
    workspace,
):
    content = "历史运行中已经持久化的完整正文。" * 80
    artifact = workspace / "Work/runs/run/drafts/module/r0/part-a.md"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text(content, encoding="utf-8")
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    marker = (
        "<persisted_result_part part_id=part-a "
        f"sha256={digest} characters={len(content)} "
        "history_only=true copy=forbidden "
        "artifact_ref=Work/runs/run/drafts/module/r0/part-a.md>"
    )
    call = LLMToolCall(
        id="legacy-marker",
        name="write_result_part",
        arguments={"part_id": "part-a", "content": marker},
    )

    restored = agent_loop_module._rehydrate_persisted_result_part_call(
        call,
        {},
        workspace,
        "run",
        "module",
    )

    assert restored.arguments["content"] == content


@pytest.mark.asyncio
async def test_verified_legacy_marker_closes_as_already_ready_without_replaying_write(
    agent_loop,
    workspace,
) -> None:
    content = "历史运行中已经持久化的完整正文。" * 80
    artifact = workspace / "Work/runs/run/drafts/module/r0/part-a.md"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text(content, encoding="utf-8")
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    marker = (
        "<persisted_result_part part_id=part-a "
        f"sha256={digest} characters={len(content)} "
        "history_only=true copy=forbidden "
        "artifact_ref=Work/runs/run/drafts/module/r0/part-a.md>"
    )
    write_part = AsyncMock()
    agent_loop.tools.get = (
        lambda name: write_part if name == "write_result_part" else None
    )
    agent_loop.tools.get_definitions.return_value = []
    agent_loop.usage_run_id = "run"
    agent_loop.usage_task_id = "module"
    agent_loop._chat_with_retries = AsyncMock(
        return_value=SimpleNamespace(
            content="The durable part is ready.",
            tool_calls=[],
            thinking=None,
        )
    )

    await agent_loop._handle_tool_calls(
        SimpleNamespace(
            content="",
            thinking=None,
            tool_calls=[
                LLMToolCall(
                    id="call-legacy-ready",
                    name="write_result_part",
                    arguments={"part_id": "part-a", "content": marker},
                )
            ],
            usage=None,
        ),
        "msg-legacy-ready",
    )

    write_part.assert_not_awaited()
    assert artifact.read_text(encoding="utf-8") == content
    followup_messages = agent_loop._chat_with_retries.await_args.args[0]
    retained_call = next(
        message.tool_calls[0]
        for message in followup_messages
        if message.role == "assistant" and message.tool_calls
    )
    assert "persisted_result_part" not in retained_call.arguments["content"]
    result = next(
        json.loads(message.content)
        for message in followup_messages
        if message.is_tool_result
    )
    assert result["status"] == "already_ready"
    assert result["ready_part_ids"] == ["part-a"]


def test_persisted_result_part_disk_fallback_rejects_cross_part_artifact(
    workspace,
):
    artifact = workspace / "Work/runs/run/drafts/module/r0/part-a.md"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text("A小节的完整正文。" * 40, encoding="utf-8")
    marker = (
        "<persisted_result_part part_id=part-b "
        f"sha256={'a' * 64} characters=999 history_only=true copy=forbidden "
        "artifact_ref=Work/runs/run/drafts/module/r0/part-a.md>"
    )
    call = LLMToolCall(
        id="cross-part-marker",
        name="write_result_part",
        arguments={"part_id": "part-b", "content": marker},
    )

    restored = agent_loop_module._rehydrate_persisted_result_part_call(
        call,
        {},
        workspace,
        "run",
        "module",
    )

    assert restored.arguments["content"] == marker


def test_persisted_result_part_disk_fallback_rejects_other_run_artifact(
    workspace,
):
    content = "另一运行的正文。" * 80
    artifact = workspace / "Work/runs/other-run/drafts/module/r0/part-a.md"
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text(content, encoding="utf-8")
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    marker = (
        "<persisted_result_part part_id=part-a "
        f"sha256={digest} characters={len(content)} "
        "history_only=true copy=forbidden "
        "artifact_ref=Work/runs/other-run/drafts/module/r0/part-a.md>"
    )
    call = LLMToolCall(
        id="other-run-marker",
        name="write_result_part",
        arguments={"part_id": "part-a", "content": marker},
    )

    restored = agent_loop_module._rehydrate_persisted_result_part_call(
        call,
        {},
        workspace,
        "current-run",
        "module",
    )

    assert restored.arguments["content"] == marker


def test_batch_marker_redaction_preserves_valid_peer_content() -> None:
    marker = (
        "<persisted_result_part part_id=part-b "
        f"sha256={'c' * 64} characters=900 "
        "history_only=true copy=forbidden>"
    )
    call = LLMToolCall(
        id="batch-marker",
        name="write_result_parts",
        arguments={
            "parts": [
                {"part_id": "part-a", "content": "完整的A部分。"},
                {"part_id": "part-b", "content": marker},
            ]
        },
    )

    assert agent_loop_module._unresolved_persisted_result_part_ids(call) == [
        "part-b"
    ]
    redacted = agent_loop_module._redact_unresolved_persisted_result_part_call(
        call
    )
    assert redacted.arguments["parts"][0]["content"] == "完整的A部分。"
    assert "content" in redacted.arguments["parts"][1]
    assert "persisted_result_part" not in redacted.arguments["parts"][1]["content"]
    assert "Regenerate complete reader-visible prose" in (
        redacted.arguments["parts"][1]["content"]
    )


def test_provider_working_history_does_not_replay_rejected_tool_arguments() -> None:
    malformed_payload = '{"kind":"cross_owner_finding_submission"'
    original = [
        LLMMessage(role="user", content="Review the current owner module."),
        LLMMessage(
            role="assistant",
            content="Review complete.",
            tool_calls=[
                LLMToolCall(
                    id="call-rejected-submit",
                    name="submit_result",
                    arguments={"payload": malformed_payload},
                )
            ],
        ),
        LLMMessage(
            role="user",
            content=json.dumps(
                {
                    "status": "correction_required",
                    "accepted": False,
                    "repair_instruction": "Pass payload as a native object.",
                }
            ),
            tool_call_id="call-rejected-submit",
            is_tool_result=True,
        ),
    ]

    working = agent_loop_module._provider_working_messages(original)

    assert original[1].tool_calls[0].arguments["payload"] == malformed_payload
    assert not any(
        call.arguments.get("payload") == malformed_payload
        for message in working
        for call in (message.tool_calls or [])
    )
    assert not any(message.is_tool_result for message in working)
    assert any(
        message.role == "user"
        and "Pass payload as a native object" in message.content
        and "invalid arguments are not an example to copy" in message.content
        for message in working
    )


def test_provider_working_history_preserves_successful_peer_tool_pair() -> None:
    messages = [
        LLMMessage(
            role="assistant",
            content="",
            tool_calls=[
                LLMToolCall(id="call-ok", name="read", arguments={"ref": "E-1"}),
                LLMToolCall(
                    id="call-bad",
                    name="submit_result",
                    arguments={"payload": "serialized"},
                ),
            ],
        ),
        LLMMessage(
            role="tool",
            content=json.dumps({"status": "completed", "accepted": True}),
            tool_call_id="call-ok",
            is_tool_result=True,
        ),
        LLMMessage(
            role="tool",
            content=json.dumps(
                {"status": "correction_required", "accepted": False}
            ),
            tool_call_id="call-bad",
            is_tool_result=True,
        ),
    ]

    working = agent_loop_module._provider_working_messages(messages)

    retained_assistant = next(message for message in working if message.tool_calls)
    assert [call.id for call in retained_assistant.tool_calls] == ["call-ok"]
    retained_results = [message for message in working if message.is_tool_result]
    assert [message.tool_call_id for message in retained_results] == ["call-ok"]
    assert any(
        message.role == "user" and "tool_name=\"submit_result\"" in message.content
        for message in working
    )


def test_provider_working_history_drops_terminal_rejection_before_resumed_task() -> None:
    invalid_findings = '[{"category":"traceability"}]'
    messages = [
        LLMMessage(
            role="assistant",
            content="Review complete.",
            tool_calls=[
                LLMToolCall(
                    id="call-terminal-rejected",
                    name="submit_result",
                    arguments={"kind": "final", "findings": invalid_findings},
                )
            ],
        ),
        LLMMessage(
            role="user",
            content=json.dumps(
                {
                    "status": "failed",
                    "accepted": False,
                    "validation_errors": [
                        {"field": "findings", "expected": "array"}
                    ],
                    "instruction": "stop_task",
                }
            ),
            tool_call_id="call-terminal-rejected",
            is_tool_result=True,
        ),
        LLMMessage(
            role="user",
            content="<task_context>resumed typed task</task_context>",
        ),
    ]

    working = agent_loop_module._provider_working_messages(messages)

    assert not any(
        call.arguments.get("findings") == invalid_findings
        for message in working
        for call in (message.tool_calls or [])
    )
    correction = next(
        message
        for message in working
        if message.role == "user" and "tool_input_correction" in message.content
    )
    assert "earlier rejected call" in correction.content
    assert "current task schema" in correction.content
    assert invalid_findings not in correction.content
    assert "stale correction example" in correction.content


def test_provider_working_history_drops_stale_correction_detail_before_resumed_task() -> None:
    stale_feedback = "OLD_EMPTY_ARRAY_EXAMPLE"
    messages = [
        LLMMessage(
            role="assistant",
            content="",
            tool_calls=[
                LLMToolCall(
                    id="call-old-correction",
                    name="submit_result",
                    arguments={"findings": "[{...}]"},
                )
            ],
        ),
        LLMMessage(
            role="user",
            content=json.dumps(
                {
                    "status": "correction_required",
                    "accepted": False,
                    "repair_instruction": stale_feedback,
                }
            ),
            tool_call_id="call-old-correction",
            is_tool_result=True,
        ),
        LLMMessage(role="user", content="<task_boundary>resume</task_boundary>"),
    ]

    working = agent_loop_module._provider_working_messages(messages)

    assert not any(
        call.arguments.get("findings") == "[{...}]"
        for message in working
        for call in (message.tool_calls or [])
    )
    assert all(stale_feedback not in str(message.content or "") for message in working)
    assert any(
        "current task schema" in str(message.content or "")
        for message in working
    )


@pytest.mark.asyncio
async def test_fabricated_result_part_marker_requests_correction_without_tool_error(
    agent_loop,
    workspace,
):
    write_part = AsyncMock()
    agent_loop.tools.get = (
        lambda name: write_part if name == "write_result_part" else None
    )
    agent_loop.tools.get_definitions.return_value = []
    agent_loop.usage_run_id = "run"
    agent_loop.usage_task_id = "module"
    agent_loop._chat_with_retries = AsyncMock(
        return_value=SimpleNamespace(
            content="I will regenerate the missing prose.",
            tool_calls=[],
            thinking=None,
        )
    )
    marker = (
        "<persisted_result_part part_id=part-new "
        f"sha256={'b' * 64} characters=4993 history_only=true copy=forbidden "
        "artifact_ref=Work/runs/run/drafts/module/r0/part-new.md>"
    )

    await agent_loop._handle_tool_calls(
        SimpleNamespace(
            content="",
            thinking=None,
            tool_calls=[
                LLMToolCall(
                    id="call-fabricated-marker",
                    name="write_result_part",
                    arguments={
                        "part_id": "part-new",
                        "content": marker,
                        "evidence_ids": [],
                    },
                )
            ],
            usage=None,
        ),
        "msg-fabricated-marker",
    )

    write_part.assert_not_awaited()
    assert not (
        workspace / "Work/runs/run/drafts/module/r0/part-new.md"
    ).exists()
    correction_messages = agent_loop._chat_with_retries.await_args.args[0]
    rejected_call = next(
        message.tool_calls[0]
        for message in correction_messages
        if message.role == "assistant" and message.tool_calls
    )
    assert "content" in rejected_call.arguments
    assert "persisted_result_part" not in rejected_call.arguments["content"]
    assert "Regenerate complete reader-visible prose" in rejected_call.arguments["content"]
    correction_result = next(
        json.loads(message.content)
        for message in correction_messages
        if message.is_tool_result
    )
    assert correction_result["status"] == "correction_required"
    assert correction_result["affected_part_ids"] == ["part-new"]
    published_results = [
        message
        for message in agent_loop.bus._queue._queue
        if isinstance(message, ToolResultMsg)
        and message.tool_name == "write_result_part"
    ]
    assert published_results[-1].error is None
    assert published_results[-1].result["status"] == "correction_required"


@pytest.mark.asyncio
async def test_failed_result_part_keeps_original_arguments_for_correction(
    workspace, config, mock_provider, mock_prompt_loader
):
    full_content = "待修正正文。" * 400
    observed_messages: list[list[LLMMessage]] = []

    async def write_part(**kwargs):
        raise ValueError("unknown evidence id")

    async def unsupported_stream(*args, **kwargs):
        raise NotImplementedError

    async def followup_chat(messages, **kwargs):
        observed_messages.append(messages)
        return LLMResponse(
            content="done",
            tool_calls=[],
            usage={"input_tokens": 20, "output_tokens": 2},
        )

    mock_provider.chat_stream = unsupported_stream
    mock_provider.chat = AsyncMock(side_effect=followup_chat)
    tools = MagicMock()
    tools.get.return_value = write_part
    tools.get_definitions.return_value = []
    loop = AgentLoop(
        agent_type=AgentType.THEORY,
        workspace=workspace,
        tools=tools,
        bus=MessageBus(),
        config=config,
        llm_provider=mock_provider,
        prompt_loader=mock_prompt_loader,
        loop_manager=None,
    )

    await loop._handle_tool_calls(
        SimpleNamespace(
            content="",
            thinking=None,
            tool_calls=[
                LLMToolCall(
                    id="call-write-part",
                    name="write_result_part",
                    arguments={
                        "part_id": "2.1.1",
                        "content": full_content,
                        "evidence_ids": ["E-missing"],
                    },
                )
            ],
            usage=None,
        ),
        "msg-1",
    )

    persisted_call = next(
        message.tool_calls[0]
        for message in observed_messages[0]
        if message.tool_calls
    )
    assert persisted_call.arguments["content"] == full_content


@pytest.mark.asyncio
async def test_apply_patch_error_payload_publishes_tool_error(
    workspace, config, mock_provider, mock_prompt_loader
):
    bus = MessageBus()

    async def patch_tool(**kwargs):
        return {
            "error": "Failed to parse patch: bad hunk",
            "path": str(workspace / "test.md"),
            "current_content": "",
        }

    tools = MagicMock()
    tools.get.return_value = patch_tool
    tools.get_definitions.return_value = []
    loop = AgentLoop(
        agent_type=AgentType.THEORY,
        workspace=workspace,
        tools=tools,
        bus=bus,
        config=config,
        llm_provider=mock_provider,
        prompt_loader=mock_prompt_loader,
        loop_manager=None,
    )
    response = SimpleNamespace(
        content="",
        thinking=None,
        tool_calls=[
            LLMToolCall(
                id="call_patch",
                name="apply_patch",
                arguments={"path": "test.md", "patch": "+hello\n"},
            )
        ],
        usage=None,
    )

    await loop._handle_tool_calls(response, "msg-1")

    published = list(bus._queue._queue)
    tool_results = [msg for msg in published if isinstance(msg, ToolResultMsg)]
    assert len(tool_results) == 1
    assert tool_results[0].tool_name == "apply_patch"
    assert tool_results[0].error == "Error executing apply_patch: Failed to parse patch: bad hunk"
    assert tool_results[0].result is None


@pytest.mark.asyncio
async def test_missing_tool_argument_returns_structured_correction_without_error(
    workspace, config, mock_provider, mock_prompt_loader
):
    bus = MessageBus()

    async def patch_tool(**kwargs):
        return {"ok": True, **kwargs}

    tools = MagicMock()
    tools.get.return_value = patch_tool
    tools.get_definitions.return_value = [
        {
            "name": "apply_patch",
            "input_schema": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "patch": {"type": "string"},
                },
                "required": ["path", "patch"],
            },
        }
    ]
    loop = AgentLoop(
        agent_type=AgentType.THEORY,
        workspace=workspace,
        tools=tools,
        bus=bus,
        config=config,
        llm_provider=mock_provider,
        prompt_loader=mock_prompt_loader,
        loop_manager=None,
    )
    response = SimpleNamespace(
        content="",
        thinking=None,
        tool_calls=[
            LLMToolCall(
                id="call_patch",
                name="apply_patch",
                arguments={"path": "test.md"},
            )
        ],
        usage=None,
    )

    await loop._handle_tool_calls(response, "msg-1")

    published = list(bus._queue._queue)
    tool_results = [msg for msg in published if isinstance(msg, ToolResultMsg)]
    assert len(tool_results) == 1
    assert tool_results[0].error is None
    correction = tool_results[0].result
    assert correction["status"] == "correction_required"
    assert correction["accepted"] is False
    assert correction["tool_name"] == "apply_patch"
    assert correction["required_argument_names"] == ["path", "patch"]
    assert correction["missing_argument_names"] == ["patch"]
    assert correction["received_argument_names"] == ["path"]
    assert correction["do_not_repeat_same_shape"] is True
    assert correction["next_action"] == (
        "call_apply_patch_once_with_complete_arguments"
    )


@pytest.mark.asyncio
async def test_submit_result_partial_arguments_reach_the_submission_gate(
    workspace, config, mock_provider, mock_prompt_loader
):
    bus = MessageBus()
    received: list[dict] = []

    async def submit_result(**kwargs):
        received.append(kwargs)
        return {
            "status": "correction_required",
            "accepted": False,
            "validation_errors": [{"field": "module_id", "problem": "Field required"}],
        }

    tools = MagicMock()
    tools.get.return_value = submit_result
    tools.get_definitions.return_value = [
        {
            "name": "submit_result",
            "input_schema": {
                "type": "object",
                "properties": {
                    "kind": {"const": "module_submission"},
                    "module_id": {"type": "string"},
                },
                "required": ["kind", "module_id"],
                "additionalProperties": False,
            },
        }
    ]
    loop = AgentLoop(
        agent_type=AgentType.THEORY,
        workspace=workspace,
        tools=tools,
        bus=bus,
        config=config,
        llm_provider=mock_provider,
        prompt_loader=mock_prompt_loader,
        loop_manager=None,
    )

    await loop._handle_tool_calls(
        SimpleNamespace(
            content="",
            thinking=None,
            tool_calls=[
                LLMToolCall(
                    id="partial-submit",
                    name="submit_result",
                    arguments={"kind": "module_submission"},
                )
            ],
            usage=None,
        ),
        "msg-partial-submit",
    )

    assert received == [{"kind": "module_submission"}]
    tool_results = [
        message for message in bus._queue._queue if isinstance(message, ToolResultMsg)
    ]
    correction = tool_results[0].result
    assert correction["status"] == "correction_required"
    assert correction["validation_errors"][0]["field"] == "module_id"
    assert "tool_name" not in correction


@pytest.mark.asyncio
async def test_streamed_submit_result_without_usage_reaches_submission_gate(
    workspace, config, mock_provider, mock_prompt_loader
):
    """The streaming response adapter must not hide SubmitResult correction."""

    bus = MessageBus()
    received: list[dict] = []

    async def submit_result(**kwargs):
        received.append(kwargs)
        return {
            "status": "correction_required",
            "accepted": False,
            "validation_errors": [{"field": "kind", "problem": "Field required"}],
        }

    tools = MagicMock()
    tools.get.return_value = submit_result
    tools.get_definitions.return_value = [
        {
            "name": "submit_result",
            "input_schema": {
                "type": "object",
                "properties": {"kind": {"const": "module_revision_submission"}},
                "required": ["kind"],
                "additionalProperties": False,
            },
        }
    ]
    loop = AgentLoop(
        agent_type=AgentType.THEORY,
        workspace=workspace,
        tools=tools,
        bus=bus,
        config=config,
        llm_provider=mock_provider,
        prompt_loader=mock_prompt_loader,
        loop_manager=None,
    )

    await loop._handle_tool_calls(
        SimpleNamespace(
            content="",
            thinking=None,
            tool_calls=[
                LLMToolCall(
                    id="streamed-empty-submit",
                    name="submit_result",
                    arguments={},
                )
            ],
        ),
        "msg-streamed-empty-submit",
    )

    assert received == [{}]
    tool_results = [
        message for message in bus._queue._queue if isinstance(message, ToolResultMsg)
    ]
    assert tool_results[0].error is None
    assert tool_results[0].result["status"] == "correction_required"


@pytest.mark.asyncio
async def test_truncated_tool_call_error_mentions_apply_patch_not_write_file(
    workspace, config, mock_provider, mock_prompt_loader
):
    bus = MessageBus()
    tools = MagicMock()
    tools.get.return_value = AsyncMock()
    tools.get_definitions.return_value = [
        {
            "name": "exec",
            "input_schema": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
            },
        }
    ]
    loop = AgentLoop(
        agent_type=AgentType.PLOTTING,
        workspace=workspace,
        tools=tools,
        bus=bus,
        config=config,
        llm_provider=mock_provider,
        prompt_loader=mock_prompt_loader,
        loop_manager=None,
    )
    response = SimpleNamespace(
        content="",
        thinking=None,
        tool_calls=[
            LLMToolCall(
                id="call_exec",
                name="exec",
                arguments={},
            )
        ],
        usage={"output_tokens": 8192},
    )

    await loop._handle_tool_calls(response, "msg-1")

    published = list(bus._queue._queue)
    tool_results = [msg for msg in published if isinstance(msg, ToolResultMsg)]
    assert len(tool_results) == 1
    assert "write them one at a time using apply_patch" in (tool_results[0].error or "")
    assert "write_file" not in (tool_results[0].error or "")


@pytest.mark.asyncio
async def test_loop_ignores_report_from_other_agent(agent_loop):
    """A ReportMessage from a different agent does not set our flag."""
    agent_loop._turn_reported = False
    await agent_loop.bus.publish(ReportMessage(
        agent_type=AgentType.PLOTTING,  # fixture loop is MAIN
        task_id="tk1",
        report_type="reply",
        content="done",
    ))
    msg = await asyncio.wait_for(agent_loop.bus._queue.get(), timeout=1)
    await agent_loop.bus._notify_subscribers(msg)
    assert agent_loop._turn_reported is False


@pytest.mark.asyncio
async def test_main_loop_enqueues_nonblocking_subagent_report(
    workspace, config, mock_provider, mock_prompt_loader
):
    from manyselves.runtime.tools.task_board import TaskBoard

    board = TaskBoard()
    board.create_task(
        AgentType.MAIN,
        AgentType.THEORY,
        "derive formulas",
        task_id="tk1",
        blocking=False,
    )
    bus = MessageBus()
    tools = MagicMock()
    tools.get_definitions.return_value = []
    loop = AgentLoop(
        agent_type=AgentType.MAIN,
        workspace=workspace,
        tools=tools,
        bus=bus,
        config=config,
        llm_provider=mock_provider,
        prompt_loader=mock_prompt_loader,
        loop_manager=None,
        task_board=board,
    )

    await loop._handle_report_message(
        ReportMessage(
            agent_type=AgentType.THEORY,
            task_id="tk1",
            report_type="reply",
            summary="Theory complete",
            content="done",
        )
    )

    queued = list(loop._message_queue._queue)
    assert len(queued) == 1
    assert queued[0].agent_type == AgentType.MAIN
    assert queued[0].source == "theory"
    assert queued[0].summary == "Theory complete"
    assert queued[0].content == "done"


def _sub_loop(workspace, config, mock_provider, mock_prompt_loader, board):
    """A sub-agent (plotting) loop with a real task board, for guard tests."""
    from manyselves.runtime.loops.agent_loop import AgentLoop
    bus = MessageBus()
    tools = MagicMock()
    tools.get_definitions.return_value = []
    loop = AgentLoop(
        agent_type=AgentType.PLOTTING,
        workspace=workspace,
        tools=tools,
        bus=bus,
        config=config,
        llm_provider=mock_provider,
        prompt_loader=mock_prompt_loader,
        loop_manager=None,
        task_board=board,
    )
    return loop


async def _drain_bus(bus) -> None:
    """Deliver queued messages to subscribers (no process_loop in tests)."""
    while True:
        try:
            msg = bus._queue.get_nowait()
        except asyncio.QueueEmpty:
            return
        await bus._notify_subscribers(msg)


@pytest.mark.asyncio
async def test_sub_guard_reprompts_when_no_report(workspace, config, mock_provider, mock_prompt_loader):
    """A sub-agent ending a Main-dispatched turn without report is re-prompted."""
    from manyselves.runtime.tools.task_board import TaskBoard
    board = TaskBoard()
    board.create_task(AgentType.MAIN, AgentType.PLOTTING, "draw", task_id="tk1")
    loop = _sub_loop(workspace, config, mock_provider, mock_prompt_loader, board)

    notices = []
    loop.bus.subscribe(SystemNotice, lambda m: notices.append(m))

    msg = UserMessage(content="draw plot", agent_type=AgentType.PLOTTING, source="main_agent")
    await loop._process_message(msg)
    await _drain_bus(loop.bus)

    # mock_provider returns text-only (no tool call), so report is never called.
    # The guard should fire and publish at least one SystemNotice before going IDLE.
    assert any("Respond" in n.content for n in notices)


@pytest.mark.asyncio
async def test_sub_guard_does_not_reprompt_completed_main_task(
    workspace, config, mock_provider, mock_prompt_loader
):
    """Completed Main-dispatched tasks should not be forced to respond again."""
    from manyselves.runtime.tools.task_board import TaskBoard
    board = TaskBoard()
    board.create_task(AgentType.MAIN, AgentType.PLOTTING, "draw", task_id="tk1")
    board.complete_task("tk1", target_agent=AgentType.PLOTTING)
    loop = _sub_loop(workspace, config, mock_provider, mock_prompt_loader, board)

    notices = []
    loop.bus.subscribe(SystemNotice, lambda m: notices.append(m))

    msg = UserMessage(
        content="draw plot follow-up",
        agent_type=AgentType.PLOTTING,
        source="main_agent",
        message_id="blocking:tk1",
    )
    await loop._process_message(msg)
    await _drain_bus(loop.bus)

    assert not any("Respond" in n.content for n in notices)


@pytest.mark.asyncio
async def test_main_guard_blocks_idle_with_blocked_tasks(workspace, config, mock_provider, mock_prompt_loader):
    """Main may not go IDLE while it has BLOCKED tasks; a SystemNotice is published."""
    from manyselves.runtime.tools.task_board import TaskBoard
    board = TaskBoard()
    bus = MessageBus()
    tools = MagicMock()
    tools.get_definitions.return_value = []
    from manyselves.runtime.loops.agent_loop import AgentLoop
    loop = AgentLoop(
        agent_type=AgentType.MAIN, workspace=workspace, tools=tools, bus=bus,
        config=config, llm_provider=mock_provider, prompt_loader=mock_prompt_loader,
        loop_manager=None, task_board=board,
    )
    board.create_task(AgentType.MAIN, AgentType.PLOTTING, "draw", task_id="tk1")
    board.block_task("tk1", target_agent=AgentType.PLOTTING)

    notices = []
    bus.subscribe(SystemNotice, lambda m: notices.append(m))

    msg = UserMessage(content="coordinate", agent_type=AgentType.MAIN, source="user")
    await loop._process_message(msg)
    await _drain_bus(bus)

    assert any("被阻塞" in n.content for n in notices)

"""Tests for agent loop processing engine."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from manyselves.config.schema import AgentDefaults
from manyselves.core.loops import agent_loop as agent_loop_module
from manyselves.core.loops.agent_loop import (
    AGENT_MAX_TOKENS_CONTINUATION_REQUIRED,
    AgentLoop,
    _canonical_failed_report_response,
    _explicit_report_continuation_run_id,
    _is_explicit_evidence_decision,
    _is_explicit_report_cancel_request,
    _is_simple_report_continuation,
    _requires_reporting_workflow_route,
)
from manyselves.core.loops.bus import MessageBus
from manyselves.core.providers.base import LLMResponse, LLMStreamChunk, LLMToolCall
from manyselves.core.providers.base import Message as LLMMessage
from manyselves.core.usage_ledger import UsageLedger
from manyselves.interfaces.types import (
    AgentResponse,
    AgentStatus,
    AgentType,
    QueueUpdateMessage,
    ReportMessage,
    SystemNotice,
    TaskUpdateMessage,
    UserMessage,
)
from manyselves.interfaces.types import (
    ToolResult as ToolResultMsg,
)


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
        from manyselves.core.providers.base import LLMStreamChunk
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


def test_report_cancel_requires_direct_end_user_instruction() -> None:
    assert _is_explicit_report_cancel_request(
        UserMessage(content="请取消当前报告任务", agent_type="main", source="user")
    )
    assert not _is_explicit_report_cancel_request(
        UserMessage(
            content="报告还在运行，考虑取消后重试",
            agent_type="main",
            source="report-workflow",
        )
    )
    assert not _is_explicit_report_cancel_request(
        UserMessage(content="为什么会主动 cancel？", agent_type="main", source="user")
    )
    assert not _is_explicit_report_cancel_request(
        UserMessage(content="不要取消当前报告任务", agent_type="main", source="user")
    )


def test_failed_report_terminal_has_deterministic_non_restart_response() -> None:
    message = UserMessage(
        content=json.dumps(
            {
                "status": "failed",
                "run_id": "report-real123",
                "error": "chief audit failed",
            }
        ),
        agent_type="main",
        source="report-workflow",
    )

    response = _canonical_failed_report_response(message)

    assert response is not None
    assert "report-real123" in response
    assert "chief audit failed" in response
    assert "本轮没有启动新运行" in response


@pytest.mark.asyncio
async def test_failed_report_terminal_allows_grounded_main_explanation(
    agent_loop, mock_provider
) -> None:
    explanation = (
        "报告任务 report-real123 在总编审计阶段失败。"
        "完整性检查发现标题层级不符合合同，因此本次没有交付文档；"
        "你可以要求从原断点恢复。"
    )

    async def explain_failure(*args, **kwargs):
        yield LLMStreamChunk(delta=explanation)
        yield LLMStreamChunk(done=True)

    mock_provider.chat_stream = explain_failure

    await agent_loop._process_message(
        UserMessage(
            content=json.dumps(
                {
                    "status": "failed",
                    "run_id": "report-real123",
                    "error": "总编审计完整性检查发现标题层级不符合合同",
                }
            ),
            agent_type="main",
            source="report-workflow",
        )
    )

    visible_responses = [
        item
        for item in agent_loop.bus._queue._queue
        if isinstance(item, AgentResponse) and not item.internal
    ]
    assert [item.content for item in visible_responses] == [explanation]


@pytest.mark.asyncio
async def test_failed_report_terminal_rejects_fake_restart_claim(
    agent_loop, mock_provider
) -> None:
    async def fake_restart(*args, **kwargs):
        yield LLMStreamChunk(
            delta="报告 report-real123 已失败，但新运行 report-fake999 已在后台启动。"
        )
        yield LLMStreamChunk(done=True)

    mock_provider.chat_stream = fake_restart

    await agent_loop._process_message(
        UserMessage(
            content=json.dumps(
                {
                    "status": "failed",
                    "run_id": "report-real123",
                    "error": "chief audit failed",
                }
            ),
            agent_type="main",
            source="report-workflow",
        )
    )

    visible_responses = [
        item
        for item in agent_loop.bus._queue._queue
        if isinstance(item, AgentResponse) and not item.internal
    ]
    assert visible_responses
    assert all("report-fake999" not in item.content for item in visible_responses)
    assert "report-real123 已失败" in visible_responses[-1].content
    assert "本轮没有启动新运行" in visible_responses[-1].content


@pytest.mark.asyncio
async def test_failed_report_terminal_blocks_all_tools(agent_loop) -> None:
    resume_tool = AsyncMock(return_value={"status": "running"})
    agent_loop.tools.get = (
        lambda name: resume_tool if name == "resume_reporting_workflow" else None
    )
    agent_loop._current_message = UserMessage(
        content=json.dumps(
            {
                "status": "failed",
                "run_id": "report-real123",
                "error": "chief audit failed",
            }
        ),
        agent_type="main",
        source="report-workflow",
    )
    response = SimpleNamespace(
        content="",
        thinking=None,
        tool_calls=[
            LLMToolCall(
                id="call-invalid-resume",
                name="resume_reporting_workflow",
                arguments={"run_id": "report-real123"},
            )
        ],
        usage=None,
    )

    await agent_loop._handle_tool_calls(response, "msg-failed-terminal")

    resume_tool.assert_not_awaited()
    results = [
        message
        for message in agent_loop.bus._queue._queue
        if isinstance(message, ToolResultMsg)
        and message.tool_name == "resume_reporting_workflow"
    ]
    assert results
    assert "explanation-only" in str(results[-1].error)


def test_simple_report_continuation_excludes_new_facts() -> None:
    assert _is_simple_report_continuation(
        UserMessage(content="继续在断点处完成", agent_type="main", source="user")
    )
    assert _is_simple_report_continuation(
        UserMessage(content="恢复这个报告", agent_type="main", source="user")
    )
    assert not _is_simple_report_continuation(
        UserMessage(
            content="继续，并把人工确认的温升数据补进去",
            agent_type="main",
            source="user",
        )
    )
    assert (
        _explicit_report_continuation_run_id(
            UserMessage(
                content="请恢复 report-f91bc1714d",
                agent_type="main",
                source="user",
            )
        )
        == "report-f91bc1714d"
    )
    assert (
        _explicit_report_continuation_run_id(
            UserMessage(
                content="恢复 report-f91bc1714d，并补充人工确认数据",
                agent_type="main",
                source="user",
            )
        )
        is None
    )
    assert (
        _explicit_report_continuation_run_id(
            UserMessage(
                content=(
                    "Editor context: file\n"
                    "Current file: Work/manifest.json\n"
                    "This may or may not be related to the current task.\n"
                    "请恢复 report-f91bc1714d"
                ),
                agent_type="main",
                source="user",
            )
        )
        == "report-f91bc1714d"
    )


def test_evidence_decision_requires_current_user_selection() -> None:
    assert _is_explicit_evidence_decision(
        UserMessage(
            content="选择 draft，保留不确定性继续起草",
            agent_type="main",
            source="user",
        ),
        "draft",
    )
    assert not _is_explicit_evidence_decision(
        UserMessage(
            content='{"status":"needs_user_decision"}',
            agent_type="main",
            source="report-workflow",
        ),
        "draft",
    )
    assert not _is_explicit_evidence_decision(
        UserMessage(
            content="继续在断点处完成",
            agent_type="main",
            source="user",
        ),
        "draft",
    )


@pytest.mark.asyncio
async def test_main_cannot_choose_evidence_decision_from_workflow_terminal(
    agent_loop
) -> None:
    resume_tool = AsyncMock(return_value={"status": "running"})
    agent_loop.tools.get = (
        lambda name: resume_tool if name == "resume_reporting_workflow" else None
    )
    agent_loop._current_message = UserMessage(
        content=json.dumps(
            {
                "status": "needs_user_decision",
                "run_id": "report-real123",
                "decision_id": "evidence-real123",
            }
        ),
        agent_type="main",
        source="report-workflow",
    )
    response = SimpleNamespace(
        content="",
        thinking=None,
        tool_calls=[
            LLMToolCall(
                id="call-unauthorized-draft",
                name="resume_reporting_workflow",
                arguments={"decision_id": "evidence-real123", "action": "draft"},
            )
        ],
        usage=None,
    )

    await agent_loop._handle_tool_calls(response, "msg-decision-terminal")

    resume_tool.assert_not_awaited()
    results = [
        message
        for message in agent_loop.bus._queue._queue
        if isinstance(message, ToolResultMsg)
        and message.tool_name == "resume_reporting_workflow"
    ]
    assert results
    assert "explicitly select" in str(results[-1].error)


def test_direct_report_operation_requires_workflow_route_before_file_reads() -> None:
    assert _requires_reporting_workflow_route(
        UserMessage(
            content="根据 Inputs 中的内容，开始写作配电安全专家报告",
            agent_type="main",
            source="user",
        )
    )
    assert _requires_reporting_workflow_route(
        UserMessage(
            content="重新生成完整报告",
            agent_type="main",
            source="user",
        )
    )
    assert not _requires_reporting_workflow_route(
        UserMessage(
            content="调查这个报告为什么失败",
            agent_type="main",
            source="user",
        )
    )
    assert not _requires_reporting_workflow_route(
        UserMessage(
            content="读取 Knowledge/说明.md",
            agent_type="main",
            source="user",
        )
    )


@pytest.mark.asyncio
async def test_main_report_route_guard_rejects_preflight_file_read(agent_loop) -> None:
    read_tool = AsyncMock(return_value={"content": "must not be read"})
    agent_loop.tools.get = lambda name: read_tool if name == "read" else None
    agent_loop._current_message = UserMessage(
        content="根据 Inputs 中的内容，开始写作配电安全专家报告",
        agent_type="main",
        source="user",
    )
    response = SimpleNamespace(
        content="",
        thinking=None,
        tool_calls=[
            LLMToolCall(
                id="call-preflight-read",
                name="read",
                arguments={"path": "Inputs", "recursive": True},
            )
        ],
        usage=None,
    )

    await agent_loop._handle_tool_calls(response, "msg-report-route")

    read_tool.assert_not_awaited()
    results = [
        message
        for message in agent_loop.bus._queue._queue
        if isinstance(message, ToolResultMsg) and message.tool_name == "read"
    ]
    assert results
    assert "must be routed before project content is read" in str(results[-1].error)


@pytest.mark.asyncio
async def test_simple_continuation_resumes_latest_real_persisted_run_without_provider(
    agent_loop, workspace, mock_provider
) -> None:
    run_id = "report-real123"
    run_root = workspace / "Work" / "runs" / run_id
    run_root.mkdir(parents=True)
    (run_root / "request.json").write_text("{}", encoding="utf-8")
    (run_root / "workflow-state.json").write_text(
        json.dumps({"run_id": run_id, "status": "failed"}),
        encoding="utf-8",
    )
    agent_loop._conversation_history.extend(
        [
            LLMMessage(
                role="user",
                content=json.dumps(
                    {
                        "status": "failed",
                        "run_id": run_id,
                        "error": "chief audit failed",
                    }
                ),
            ),
            LLMMessage(
                role="assistant",
                content=(
                    "报告任务 report-fake999 已在后台启动。"
                ),
            ),
        ]
    )
    resume_tool = AsyncMock(
        return_value={
            "status": "running",
            "run_id": run_id,
            "task_id": "task-resume",
        }
    )
    agent_loop.tools.get = (
        lambda name: resume_tool if name == "resume_reporting_workflow" else None
    )

    await agent_loop._process_message(
        UserMessage(
            content="继续在断点处完成",
            agent_type="main",
            source="user",
        )
    )

    resume_tool.assert_awaited_once_with(run_id=run_id)
    mock_provider.chat.assert_not_awaited()
    responses = [
        item
        for item in agent_loop.bus._queue._queue
        if isinstance(item, AgentResponse) and not item.streaming
    ]
    assert responses
    assert run_id in responses[-1].content
    assert "report-fake999" not in responses[-1].content


@pytest.mark.asyncio
async def test_explicit_continuation_resumes_named_persisted_run_without_history(
    agent_loop, workspace
) -> None:
    run_id = "report-f91bc1714d"
    run_root = workspace / "Work" / "runs" / run_id
    run_root.mkdir(parents=True)
    (run_root / "request.json").write_text("{}", encoding="utf-8")
    (run_root / "workflow-state.json").write_text(
        json.dumps({"run_id": run_id, "status": "failed"}),
        encoding="utf-8",
    )
    resume_tool = AsyncMock(
        return_value={"status": "running", "run_id": run_id, "task_id": "task-resume"}
    )
    agent_loop.tools.get = (
        lambda name: resume_tool if name == "resume_reporting_workflow" else None
    )

    await agent_loop._process_message(
        UserMessage(
            content=f"请恢复 {run_id}",
            agent_type="main",
            source="user",
        )
    )

    resume_tool.assert_awaited_once_with(run_id=run_id)


@pytest.mark.asyncio
async def test_editor_context_does_not_hide_explicit_resume_command(
    agent_loop, workspace, mock_provider
) -> None:
    run_id = "report-f91bc1714d"
    run_root = workspace / "Work" / "runs" / run_id
    run_root.mkdir(parents=True)
    (run_root / "request.json").write_text("{}", encoding="utf-8")
    (run_root / "workflow-state.json").write_text(
        json.dumps({"run_id": run_id, "status": "failed"}),
        encoding="utf-8",
    )
    resume_tool = AsyncMock(
        return_value={"status": "running", "run_id": run_id, "task_id": "task-resume"}
    )
    agent_loop.tools.get = (
        lambda name: resume_tool if name == "resume_reporting_workflow" else None
    )

    await agent_loop._process_message(
        UserMessage(
            content=(
                "Editor context: file\n"
                "Current file: Work/manifest.json\n"
                "This may or may not be related to the current task.\n"
                f"请恢复 {run_id}"
            ),
            agent_type="main",
            source="user",
        )
    )

    resume_tool.assert_awaited_once_with(run_id=run_id)
    mock_provider.chat.assert_not_awaited()


@pytest.mark.asyncio
async def test_invalid_explicit_resume_never_falls_back_to_new_run(
    agent_loop, mock_provider
) -> None:
    run_tool = AsyncMock(return_value={"status": "running", "run_id": "report-new"})
    agent_loop.tools.get = (
        lambda name: run_tool if name == "run_reporting_workflow" else None
    )

    await agent_loop._process_message(
        UserMessage(
            content="请恢复 report-missing",
            agent_type="main",
            source="user",
        )
    )

    run_tool.assert_not_awaited()
    mock_provider.chat.assert_not_awaited()
    responses = [
        item
        for item in agent_loop.bus._queue._queue
        if isinstance(item, AgentResponse) and not item.streaming
    ]
    assert responses
    assert "不能从断点恢复" in responses[-1].content
    assert "没有新建报告运行" in responses[-1].content


@pytest.mark.asyncio
async def test_bare_continue_without_real_terminal_never_starts_new_run(
    agent_loop, mock_provider
) -> None:
    await agent_loop._process_message(
        UserMessage(content="继续", agent_type="main", source="user")
    )

    mock_provider.chat.assert_not_awaited()
    responses = [
        item
        for item in agent_loop.bus._queue._queue
        if isinstance(item, AgentResponse) and not item.streaming
    ]
    assert responses
    assert "没有新建报告运行" in responses[-1].content


@pytest.mark.asyncio
async def test_direct_report_request_cannot_publish_fake_start_without_tool_receipt(
    agent_loop, mock_provider
) -> None:
    async def fake_start_without_tool(*args, **kwargs):
        yield LLMStreamChunk(
            delta="报告已启动，run_id=report-fake999。",
        )
        yield LLMStreamChunk(done=True)

    mock_provider.chat_stream = fake_start_without_tool
    agent_loop.tools.get.return_value = None

    await agent_loop._process_message(
        UserMessage(
            content="开始生成配电安全评估报告",
            agent_type="main",
            source="user",
        )
    )

    visible_responses = [
        item
        for item in agent_loop.bus._queue._queue
        if isinstance(item, AgentResponse) and not item.internal
    ]
    assert visible_responses
    assert all("report-fake999" not in item.content for item in visible_responses)
    assert "没有启动、恢复或修订任何报告" in visible_responses[-1].content


@pytest.mark.asyncio
async def test_background_report_start_ends_main_turn_without_followup_tool_loop(
    workspace, config, mock_provider, mock_prompt_loader
):
    bus = MessageBus()

    async def start_report(**kwargs):
        return {
            "status": "running",
            "run_id": "report-123",
            "task_id": "task-123",
        }

    tools = MagicMock()
    tools.get.return_value = start_report
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
                id="call_report",
                name="run_reporting_workflow",
                arguments={"operation": "full_report", "instruction": "生成完整报告"},
            )
        ],
        usage=None,
    )

    await loop._handle_tool_calls(response, "msg-report")

    mock_provider.chat.assert_not_called()
    responses = [
        msg for msg in bus._queue._queue
        if isinstance(msg, AgentResponse) and not msg.streaming
    ]
    assert responses
    assert "等待工作流终态回传" in responses[-1].content


@pytest.fixture
def agent_loop(workspace, config, mock_gui, mock_provider, mock_prompt_loader):
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
        loop_manager=None,  # Not needed for tests
    )
    return loop


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
    from manyselves.core.tools.task_board import TaskBoard

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
        for message in list(agent_loop.bus._queue._queue)
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
        from manyselves.core.providers.base import LLMStreamChunk

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
        from manyselves.core.providers.base import LLMStreamChunk
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
async def test_successful_batch_result_parts_compact_each_large_content_in_provider_history(
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
    compacted_parts = assistant_call.arguments["parts"]
    assert all(
        part["content"].startswith("<persisted_result_part sha256=")
        for part in compacted_parts
    )
    assert first_content not in compacted_parts[0]["content"]
    assert second_content not in compacted_parts[1]["content"]
    assert (
        "artifact_ref=Work/runs/run/result-parts/part-a.md"
        in compacted_parts[0]["content"]
    )
    assert (
        "artifact_ref=Work/runs/run/result-parts/part-b.md"
        in compacted_parts[1]["content"]
    )


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


def test_working_memory_compaction_persists_removed_transcript(agent_loop, workspace):
    messages = [
        LLMMessage(role="system", content="system"),
        LLMMessage(role="user", content="original task" + ("x" * 160000)),
        LLMMessage(role="user", content="recent task state"),
    ]

    compacted = agent_loop._compact_working_memory(messages)
    agent_loop._compact_working_memory(messages)

    assert len(compacted) < len(messages) + 1
    checkpoints = list(
        (workspace / ".manyselves/artifacts/context-checkpoint").glob("*.txt")
    )
    assert len(checkpoints) == 1
    assert "original task" in checkpoints[0].read_text(encoding="utf-8")
    assert "checkpoint_ref=artifact:v1:" in compacted[1].content
    assert not (workspace / ".manyselves/context-checkpoints").exists()


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

    published = list(bus._queue._queue)
    assert [
        msg.thinking
        for msg in published
        if isinstance(msg, AgentResponse) and msg.thinking
    ] == ["checking result"]


@pytest.mark.asyncio
async def test_successful_result_part_replaces_persisted_prose_in_followup_history(
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
    assert full_content not in persisted_call.arguments["content"]
    assert "persisted_result_part" in persisted_call.arguments["content"]
    assert "artifact_ref=Work/runs/run-1/drafts/module-2.1/r0/2.1.1.md" in (
        persisted_call.arguments["content"]
    )
    assert persisted_call.arguments["evidence_ids"] == []
    assert original_call.arguments["content"] == full_content


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
async def test_missing_apply_patch_argument_reports_required_schema(
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
    assert "requires arguments: path, patch" in (tool_results[0].error or "")
    assert "missing required arguments: patch" in (tool_results[0].error or "")


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
    from manyselves.core.tools.task_board import TaskBoard

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
    from manyselves.core.loops.agent_loop import AgentLoop
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
    from manyselves.core.tools.task_board import TaskBoard
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
    from manyselves.core.tools.task_board import TaskBoard
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
    from manyselves.core.tools.task_board import TaskBoard
    board = TaskBoard()
    bus = MessageBus()
    tools = MagicMock()
    tools.get_definitions.return_value = []
    from manyselves.core.loops.agent_loop import AgentLoop
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

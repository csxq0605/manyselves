from types import SimpleNamespace

from manyselves.core.conversations import ConversationStore
from manyselves.gui.main_window import MainWindow
from manyselves.gui.widgets.agent_sidebar import AgentSidebar
from manyselves.interfaces.types import AgentResponse, AgentStatus, StatusChange, ToolCallMessage, UserMessage


def test_runtime_agent_events_create_separate_visible_history(qtbot, tmp_path):
    runtime_id = "module-2.4-specialist--session-abcdef123456"
    store = ConversationStore(tmp_path)
    sidebar = AgentSidebar()
    qtbot.addWidget(sidebar)

    fake = SimpleNamespace(
        _agent_status_cache={},
        _agent_stream_buffers={},
        _turn_state={},
        _conv_store=store,
        agent_sidebar=sidebar,
        current_agent_type="main",
    )
    fake._is_visible_agent = lambda agent_id: agent_id == fake.current_agent_type
    fake._ensure_agent_visible = lambda agent_id, status=None: MainWindow._ensure_agent_visible(
        fake, agent_id, status
    )
    fake._state_for_agent = lambda agent_id: MainWindow._state_for_agent(fake, agent_id)

    MainWindow._handle_user_message(
        fake,
        UserMessage(
            agent_type=runtime_id,
            source="workflow",
            message_id="module-2.4",
            content="Analyze the equipment-risk module.",
        ),
    )
    MainWindow._handle_agent_response(
        fake,
        AgentResponse(
            agent_type=runtime_id,
            message_id="module-2.4",
            content="Inspecting the existing draft. ",
            streaming=True,
        ),
    )
    MainWindow._handle_agent_response(
        fake,
        AgentResponse(
            agent_type=runtime_id,
            message_id="module-2.4",
            content="Preparing Render handoff.",
            streaming=True,
        ),
    )
    MainWindow._handle_tool_call(
        fake,
        ToolCallMessage(
            agent_type=runtime_id,
            tool_name="open_artifact",
            arguments={"ref": "draft"},
        ),
    )

    assert runtime_id in sidebar.agent_ids()
    records = store.load_messages(runtime_id)
    assert [record["role"] for record in records] == ["user", "agent", "tool_call"]
    assert records[0]["content"] == "Analyze the equipment-risk module."
    assert records[1]["content"] == (
        "Inspecting the existing draft. Preparing Render handoff."
    )
    assert records[2]["content"] == "open_artifact"


def test_conversation_store_rediscovers_runtime_agent_history(tmp_path):
    runtime_id = "evidence-auditor--session-112233445566"
    first = ConversationStore(tmp_path)
    first.append_message(runtime_id, "agent", "Audit in progress")

    reopened = ConversationStore(tmp_path)

    assert runtime_id in reopened.get_agent_types_with_history()
    assert reopened.load_messages(runtime_id)[0]["content"] == "Audit in progress"


def test_new_report_run_replaces_previous_runtime_rows_without_deleting_history(qtbot, tmp_path):
    old_runtime = "module-2.4-specialist--session-old"
    store = ConversationStore(tmp_path)
    store.append_message(old_runtime, "agent", "old run history")
    sidebar = AgentSidebar()
    qtbot.addWidget(sidebar)
    sidebar.ensure_agent("main")
    sidebar.ensure_agent("report-workflow")
    sidebar.ensure_agent(old_runtime)
    fake = SimpleNamespace(
        _agent_status_cache={old_runtime: ("thinking", {})},
        _agent_queue_cache={old_runtime: ["old task"]},
        _turn_state={old_runtime: object()},
        _agent_stream_buffers={old_runtime: "old stream"},
        _conv_store=store,
        agent_sidebar=sidebar,
        current_agent_type=old_runtime,
    )
    fake._ensure_agent_visible = lambda agent_id, status=None: MainWindow._ensure_agent_visible(
        fake, agent_id, status
    )
    fake._is_visible_agent = lambda agent_id: agent_id == fake.current_agent_type

    MainWindow._handle_status_change(
        fake,
        StatusChange(
            agent_type="report-workflow",
            status=AgentStatus.THINKING,
            extra={"run_id": "report-new", "task": "new report"},
        ),
    )

    assert sidebar.agent_ids() == ["main", "report-workflow"]
    assert fake.current_agent_type == "main"
    assert old_runtime not in fake._agent_status_cache
    assert old_runtime not in fake._agent_queue_cache
    assert old_runtime not in fake._turn_state
    assert old_runtime not in fake._agent_stream_buffers
    assert store.load_messages(old_runtime)[0]["content"] == "old run history"

    new_runtime = "module-2.1-specialist--session-new"
    MainWindow._handle_status_change(
        fake,
        StatusChange(agent_type=new_runtime, status=AgentStatus.THINKING),
    )
    assert sidebar.agent_ids() == ["main", "report-workflow", new_runtime]

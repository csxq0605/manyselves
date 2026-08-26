from types import SimpleNamespace
from uuid import UUID

import pytest

from manyselves.application.main_workflow_tool import (
    ManageWorkflowsTool,
    attach_main_workflow_tool,
)


class _Projection:
    def __init__(self) -> None:
        self.started: list[tuple[UUID, str, object]] = []

    def list_workflows(self) -> list[dict[str, object]]:
        return [
            {"id": "full-report", "runnable": True},
            {"id": "internal-child", "runnable": False},
        ]

    def input_schema(self, workflow_id: str) -> dict[str, object]:
        return {
            "workflow_id": workflow_id,
            "schema": {"type": "object", "required": ["instruction"]},
        }

    async def start(
        self,
        command_id: UUID,
        workflow_id: str,
        values: object,
    ) -> dict[str, object]:
        self.started.append((command_id, workflow_id, values))
        return {
            "status": "accepted",
            "run_id": "full-report-command",
            "workflow_id": workflow_id,
            "capability_id": "distribution-reporting",
        }


class _Conversations:
    def __init__(self) -> None:
        self.bound: list[str] = []

    def bind_run_to_active_conversation(self, run_id: str) -> str:
        self.bound.append(run_id)
        return "conversation-main"


@pytest.mark.asyncio
async def test_main_workflow_tool_lists_schema_starts_and_binds_current_conversation() -> None:
    projection = _Projection()
    conversations = _Conversations()
    tool = ManageWorkflowsTool(
        projection_resolver=lambda: projection,
        conversation_resolver=lambda: conversations,
    )

    listed = await tool(action="list")
    schema = await tool(action="schema", workflow_id="full-report")
    started = await tool(
        action="start",
        workflow_id="full-report",
        input={"instruction": "Generate the complete report."},
    )

    assert listed == {
        "status": "ok",
        "workflows": [{"id": "full-report", "runnable": True}],
    }
    assert schema["workflow_id"] == "full-report"
    assert projection.started[0][1:] == (
        "full-report",
        {"instruction": "Generate the complete report."},
    )
    assert conversations.bound == ["full-report-command"]
    assert started["run_id"] == "full-report-command"
    assert started["conversation_id"] == "conversation-main"


def test_application_attaches_generic_workflow_tool_to_main_runtime() -> None:
    registered: list[tuple[str, object]] = []
    manager = SimpleNamespace(
        register_agent_tool=lambda agent_id, tool: registered.append((agent_id, tool)) or True
    )
    host = SimpleNamespace(loop_manager=manager)

    attached = attach_main_workflow_tool(
        host,
        projection_resolver=lambda: _Projection(),
        conversation_resolver=lambda: _Conversations(),
    )

    assert attached is True
    assert registered[0][0] == "main"
    assert registered[0][1].name == "manage_workflows"

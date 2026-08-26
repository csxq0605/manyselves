from types import SimpleNamespace
from uuid import UUID

import pytest

from manyselves.capabilities.distribution_reporting.adapters.main_tool import (
    RunReportingWorkflowTool,
    attach_main_reporting_tool,
)
from manyselves.runtime.tools.registry import ToolRegistry


class _Projection:
    def __init__(self) -> None:
        self.started: list[tuple[UUID, str, object]] = []

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
async def test_distribution_demo_main_starts_full_report_without_workflow_discovery() -> None:
    projection = _Projection()
    conversations = _Conversations()
    tool = RunReportingWorkflowTool(
        projection_resolver=lambda: projection,
        conversation_resolver=lambda: conversations,
    )

    result = await tool(
        instruction="Generate the complete report from the project Inputs.",
        operation="full_report",
        missing_evidence_policy="draft",
        cost_control_mode="observe",
        preparation_mode="deterministic_workers",
        preparation_concurrency=4,
    )

    assert tool.name == "run_reporting_workflow"
    assert projection.started[0][1] == "full-report"
    assert projection.started[0][2] == {
        "instruction": "Generate the complete report from the project Inputs.",
        "target_modules": ["2.1", "2.2", "2.3", "2.4", "2.5"],
        "source_module_refs": None,
        "source_markdown_ref": None,
        "output_filename": None,
        "execution_requirements": [],
        "user_supplements": [],
        "missing_evidence_policy": "draft",
        "cost_control_mode": "observe",
        "max_provider_attempts": 80,
        "max_total_tokens": 800_000,
        "preparation_mode": "deterministic_workers",
        "preparation_concurrency": 4,
    }
    assert conversations.bound == ["full-report-command"]
    assert result["run_id"] == "full-report-command"
    assert result["conversation_id"] == "conversation-main"


def test_distribution_demo_attaches_reporting_tool_to_main_runtime() -> None:
    registered: list[tuple[str, object]] = []
    manager = SimpleNamespace(
        register_agent_tool=lambda agent_id, tool: registered.append(
            (agent_id, tool)
        )
        or True
    )
    host = SimpleNamespace(loop_manager=manager)

    attached = attach_main_reporting_tool(
        host,
        projection_resolver=lambda: _Projection(),
        conversation_resolver=lambda: _Conversations(),
    )

    assert attached is True
    assert registered[0][0] == "main"
    assert registered[0][1].name == "run_reporting_workflow"


def test_distribution_demo_main_tool_exposes_the_five_operation_contract() -> None:
    registry = ToolRegistry()
    registry.register(
        RunReportingWorkflowTool(
            projection_resolver=lambda: _Projection(),
            conversation_resolver=lambda: _Conversations(),
        )
    )

    definition = registry.get_definitions()[0]
    assert definition["name"] == "run_reporting_workflow"
    assert definition["input_schema"]["properties"]["operation"]["enum"] == [
        "distill_template_skill",
        "full_report",
        "module_report",
        "aggregate_existing",
        "render_existing",
    ]

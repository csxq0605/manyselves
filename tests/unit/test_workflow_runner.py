import asyncio
from collections.abc import Mapping
from pathlib import Path

import pytest

from pds_report.agents.base import AgentContext
from pds_report.domain.models import RunStatus
from pds_report.workflow.bus import MessageBus
from pds_report.workflow.config import AgentDefinition, PhaseDefinition, WorkflowDefinition
from pds_report.workflow.events import EventType
from pds_report.workflow.runner import WorkflowRunner
from pds_report.workflow.tasks import TaskBoard, TaskStatus


def definition(
    agent_id: str,
    *,
    reads: list[str] | None = None,
    writes: list[str] | None = None,
) -> AgentDefinition:
    return AgentDefinition(
        id=agent_id,
        role="test",
        reads=reads or [],
        writes=writes or [],
        tools=[],
        instructions="Test agent.",
    )


class StaticAgent:
    def __init__(self, output: Mapping[str, object]) -> None:
        self.output = output

    async def run(self, context: AgentContext) -> Mapping[str, object]:
        return self.output


class IncrementingAgent:
    async def run(self, context: AgentContext) -> Mapping[str, object]:
        return {"run_summary": {"number": context.state["run_summary"]["number"] + 1}}


class FailingAgent:
    async def run(self, context: AgentContext) -> Mapping[str, object]:
        raise RuntimeError("agent exploded")


def context() -> AgentContext:
    return AgentContext(
        project_root=Path(".test-projects/runner"),
        run_id="run-1",
        state={},
        bus=MessageBus(),
        task_board=TaskBoard(),
    )


@pytest.mark.asyncio
async def test_pipeline_passes_state_to_next_agent() -> None:
    definitions = {
        "first": definition("first", writes=["run_summary"]),
        "second": definition("second", reads=["run_summary"], writes=["run_summary"]),
    }
    workflow = WorkflowDefinition(
        id="pipeline-flow",
        phases=[
            PhaseDefinition(
                id="phase",
                mode="pipeline",
                agents=["first", "second"],
            )
        ],
    )

    result = await WorkflowRunner().run(
        workflow,
        definitions,
        {
            "first": StaticAgent({"run_summary": {"number": 1}}),
            "second": IncrementingAgent(),
        },
        context(),
    )

    assert result.status is RunStatus.COMPLETED
    assert result.state["run_summary"] == {"number": 2}


@pytest.mark.asyncio
async def test_parallel_agents_really_overlap() -> None:
    both_started = asyncio.Event()
    started: list[str] = []

    class CoordinatedAgent:
        def __init__(self, agent_id: str, output_key: str) -> None:
            self.agent_id = agent_id
            self.output_key = output_key

        async def run(self, context: AgentContext) -> Mapping[str, object]:
            started.append(self.agent_id)
            if len(started) == 2:
                both_started.set()
            await asyncio.wait_for(both_started.wait(), timeout=1)
            return {self.output_key: self.agent_id}

    definitions = {
        "one": definition("one", writes=["run_summary"]),
        "two": definition("two", writes=["output_artifacts"]),
    }
    workflow = WorkflowDefinition(
        id="parallel-flow",
        phases=[PhaseDefinition(id="phase", mode="parallel", agents=["one", "two"])],
    )

    result = await WorkflowRunner().run(
        workflow,
        definitions,
        {
            "one": CoordinatedAgent("one", "run_summary"),
            "two": CoordinatedAgent("two", "output_artifacts"),
        },
        context(),
    )

    assert result.status is RunStatus.COMPLETED
    assert started == ["one", "two"]


@pytest.mark.asyncio
async def test_parallel_write_conflict_fails_run() -> None:
    definitions = {
        "one": definition("one", writes=["run_summary"]),
        "two": definition("two", writes=["run_summary"]),
    }
    workflow = WorkflowDefinition(
        id="conflict-flow",
        phases=[PhaseDefinition(id="phase", mode="parallel", agents=["one", "two"])],
    )
    run_context = context()

    result = await WorkflowRunner().run(
        workflow,
        definitions,
        {
            "one": StaticAgent({"run_summary": "one"}),
            "two": StaticAgent({"run_summary": "two"}),
        },
        run_context,
    )

    assert result.status is RunStatus.FAILED
    assert "parallel write conflict" in result.errors[0]
    assert EventType.RUN_COMPLETED not in [event.type for event in run_context.bus.history]


@pytest.mark.asyncio
async def test_failed_agent_skips_dependent_phase() -> None:
    definitions = {
        "bad": definition("bad", writes=["run_summary"]),
        "later": definition("later", reads=["run_summary"], writes=["output_artifacts"]),
    }
    workflow = WorkflowDefinition(
        id="failure-flow",
        phases=[
            PhaseDefinition(id="first", mode="pipeline", agents=["bad"]),
            PhaseDefinition(
                id="second",
                mode="pipeline",
                agents=["later"],
                needs=["first"],
            ),
        ],
    )
    run_context = context()

    result = await WorkflowRunner().run(
        workflow,
        definitions,
        {"bad": FailingAgent(), "later": StaticAgent({"output_artifacts": []})},
        run_context,
    )

    assert result.status is RunStatus.FAILED
    assert "agent exploded" in result.errors[0]
    assert run_context.task_board.get("first:bad").status is TaskStatus.FAILED
    assert run_context.task_board.get("second:later").status is TaskStatus.SKIPPED
    event_types = [event.type for event in run_context.bus.history]
    assert EventType.RUN_FAILED in event_types
    assert EventType.RUN_COMPLETED not in event_types


@pytest.mark.asyncio
async def test_agent_cannot_write_undeclared_carrier() -> None:
    definitions = {"bad": definition("bad", writes=["run_summary"])}
    workflow = WorkflowDefinition(
        id="contract-flow",
        phases=[PhaseDefinition(id="phase", mode="pipeline", agents=["bad"])],
    )

    result = await WorkflowRunner().run(
        workflow,
        definitions,
        {"bad": StaticAgent({"output_artifacts": []})},
        context(),
    )

    assert result.status is RunStatus.FAILED
    assert "undeclared carriers" in result.errors[0]

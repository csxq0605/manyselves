"""Stopped runs: temporary failures resume on their own, the rest reach Main."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from manyselves.application.main_run_tools import GetRunStatusTool, ResumeRunTool
from manyselves.application.run_supervisor import RunSupervisor
from manyselves.kernel.workflow import ResolvedPlan
from manyselves.kernel.workflow.models import InvokeAgentAction, InvokeToolAction, SubworkflowAction
from manyselves.runtime.run_failure import describe_failure


def _plan() -> ResolvedPlan:
    child = ResolvedPlan(
        workflow_id="child",
        workflow_version="1",
        actions=[
            InvokeToolAction(id="prepare", tool="t", input_variable="a", output_variable="b"),
            InvokeAgentAction(
                id="judge", agent="a", task="t", conversation_variable="c",
                input_variable="a", output_variable="b",
            ),
        ],
    )
    return ResolvedPlan(
        workflow_id="root",
        workflow_version="1",
        actions=[
            SubworkflowAction(
                id="run-child", workflow="child", input_variable="a", output_variable="b"
            )
        ],
        subworkflow_plans={"child": child},
    )


def _state(action: str, error: str) -> dict[str, Any]:
    return {
        "status": "failed",
        "actions": {"run-child": {"status": "failed", "error": error}},
        "subworkflow_states": {
            "run-child": {
                "status": "failed",
                "actions": {action: {"status": "failed", "error": error}},
            }
        },
    }


def test_failure_is_located_and_classified_from_structure_and_text() -> None:
    plan = _plan()

    model = describe_failure(plan, _state("judge", "no progress after correction"))
    temporary = describe_failure(plan, _state("judge", "HTTP 529 overloaded"))
    program = describe_failure(plan, _state("prepare", "KeyError: 'rows'"))
    network = describe_failure(plan, _state("prepare", "Connection reset by peer"))

    assert model is not None and model.path == ["run-child", "judge"]
    assert (model.category, model.retryable) == ("model", True)
    assert (temporary.category, temporary.retryable) == ("temporary", True)
    assert (program.category, program.retryable) == ("program", False)
    assert program.action_kind == "invoke_tool"
    assert network.category == "temporary"
    assert describe_failure(plan, {"status": "completed"}) is None


class _Projection:
    def __init__(self, failure_state: dict[str, Any]) -> None:
        self.status = "failed"
        self.failure_state = failure_state
        self.resumed: list[str] = []
        self.supervisor: RunSupervisor | None = None

    def get_run(self, run_id: str) -> dict[str, Any]:
        return {"run": {"run_id": run_id, "workflow_id": "root", "status": self.status}}

    def describe_failure(self, run_id: str):
        return describe_failure(_plan(), self.failure_state)

    def versions(self, run_id: str) -> list[dict[str, Any]]:
        return [{"version": 1}]

    async def resume(self, command_id, run_id: str, *, automatic: bool = False):
        self.resumed.append(run_id)
        return {"status": "accepted", "run_id": run_id}


@pytest.mark.asyncio
async def test_model_failure_resumes_twice_then_tells_main() -> None:
    projection = _Projection(_state("judge", "no usable result"))
    notices: list[str] = []

    async def notify(text: str) -> None:
        notices.append(text)

    supervisor = RunSupervisor(projection, notify, resume_delay_seconds=0)

    for _ in range(3):
        await supervisor.on_settled("run-1")
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    assert projection.resumed == ["run-1", "run-1"]
    assert len(notices) == 1
    assert "已自动续跑 2 次" in notices[0] and "run-child / judge" in notices[0]
    assert 'status="失败"' in notices[0]


@pytest.mark.asyncio
async def test_program_failure_is_not_resumed() -> None:
    projection = _Projection(_state("prepare", "KeyError: 'rows'"))
    notices: list[str] = []

    async def notify(text: str) -> None:
        notices.append(text)

    supervisor = RunSupervisor(projection, notify, resume_delay_seconds=0)
    await supervisor.on_settled("run-1")

    assert projection.resumed == []
    assert "程序步骤出错" in notices[0] and "需要先修代码或输入" in notices[0]


@pytest.mark.asyncio
async def test_completion_is_reported_with_the_automatic_resumes() -> None:
    projection = _Projection(_state("judge", "timeout"))
    notices: list[str] = []

    async def notify(text: str) -> None:
        notices.append(text)

    supervisor = RunSupervisor(projection, notify, resume_delay_seconds=0)
    await supervisor.on_settled("run-1")
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    projection.status = "completed"
    await supervisor.on_settled("run-1")

    assert projection.resumed == ["run-1"]
    assert "已完成" in notices[-1] and "自动续跑 1 次" in notices[-1]
    assert supervisor.auto_resumes("run-1") == 0


@pytest.mark.asyncio
async def test_main_tools_read_status_and_resume() -> None:
    projection = _Projection(_state("prepare", "KeyError: 'rows'"))
    status = await GetRunStatusTool(projection_resolver=lambda: projection)(run_id="run-1")
    resumed = await ResumeRunTool(projection_resolver=lambda: projection)(run_id="run-1")

    assert status["failure"]["category"] == "program"
    assert status["versions"] == [{"version": 1}]
    assert resumed["run_id"] == "run-1" and projection.resumed == ["run-1"]

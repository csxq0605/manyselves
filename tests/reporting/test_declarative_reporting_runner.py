from pathlib import Path

import pytest

from manyselves.core.loops.bus import MessageBus
from manyselves.core.providers.base import LLMProvider
from manyselves.core.reporting.declarative_reporting_runner import (
    DeclarativeReportWorkflowRunner,
    execute_declarative_module_stage,
)
from manyselves.core.reporting.models import ReportRequest
from manyselves.core.reporting.service import ReportingRunResult, ReportingService
from manyselves.core.reporting.workflow import ReportWorkflowRunner
from manyselves.core.tools.task_board import TaskBoard
from manyselves.kernel.workflow import WorkflowStatus
from manyselves.runtime.state_store import FileWorkflowStateStore


@pytest.mark.asyncio
async def test_declarative_module_stage_runs_the_current_complete_cohort_as_an_adapter(
    tmp_path: Path,
) -> None:
    state = {"run_id": "report-declarative-module", "completed": []}
    calls: list[tuple[str, ...]] = []

    async def execute_current(requested_modules, current_state, workflow_id) -> None:
        calls.append(tuple(requested_modules))
        current_state["completed"] = list(requested_modules)
        current_state["workflow_id"] = workflow_id

    completed = await execute_declarative_module_stage(
        execute_current=execute_current,
        requested_modules=("2.1", "2.2"),
        state=state,
        workflow_id="full-power-distribution-report:report-declarative-module",
        state_store=FileWorkflowStateStore(tmp_path),
    )

    assert calls == [("2.1", "2.2")]
    assert state["completed"] == ["2.1", "2.2"]
    assert completed.status is WorkflowStatus.COMPLETED
    assert completed.outputs == {"result": "report-declarative-module"}


@pytest.mark.asyncio
async def test_declarative_module_stage_resumes_the_same_failed_action(
    tmp_path: Path,
) -> None:
    state = {"run_id": "report-declarative-resume"}
    calls = 0

    async def execute_current(_modules, current_state, _workflow_id) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("injected module failure")
        current_state["module_stage"] = "completed"

    store = FileWorkflowStateStore(tmp_path)
    with pytest.raises(RuntimeError, match="injected module failure"):
        await execute_declarative_module_stage(
            execute_current=execute_current,
            requested_modules=("2.1",),
            state=state,
            workflow_id="workflow-resume",
            state_store=store,
        )

    completed = await execute_declarative_module_stage(
        execute_current=execute_current,
        requested_modules=("2.1",),
        state=state,
        workflow_id="workflow-resume",
        state_store=store,
    )

    assert calls == 2
    assert state["module_stage"] == "completed"
    assert completed.status is WorkflowStatus.COMPLETED


class _NoCallProvider(LLMProvider):
    def __init__(self) -> None:
        super().__init__("test", model="no-call")

    async def chat(self, *args, **kwargs):
        raise AssertionError("runner selection must not call the Provider")


@pytest.mark.asyncio
async def test_reporting_service_selects_runner_from_run_identity_and_keeps_legacy_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selected: list[str] = []

    async def run_legacy(_runner, _state) -> None:
        selected.append("legacy")

    async def run_declarative(_runner, _state) -> None:
        selected.append("declarative")

    monkeypatch.setattr(ReportWorkflowRunner, "run", run_legacy)
    monkeypatch.setattr(DeclarativeReportWorkflowRunner, "run", run_declarative)
    service = ReportingService(
        tmp_path,
        bus=MessageBus(),
        task_board=TaskBoard(),
        llm_provider=_NoCallProvider(),
    )
    monkeypatch.setattr(
        service,
        "_finalize_completed_run",
        lambda result: result,
    )
    request = ReportRequest(
        operation="module_report",
        instruction="Select the requested runtime",
        target_modules=["2.1"],
    )

    legacy = await service._execute_locked(request, "report-legacy-selection")
    declarative = await service._execute_locked(
        request,
        "report-declarative-selection",
    )

    assert legacy == ReportingRunResult(
        run_id="report-legacy-selection",
        status="completed",
    )
    assert declarative == ReportingRunResult(
        run_id="report-declarative-selection",
        status="completed",
    )
    assert selected == ["legacy", "declarative"]

import json
from pathlib import Path

import pytest

from manyselves.core.loops.bus import MessageBus
from manyselves.core.providers.base import LLMProvider
from manyselves.core.reporting.agentic_models import ModuleSubmission
from manyselves.core.reporting.declarative_reporting_runner import (
    DeclarativeReportWorkflowRunner,
    execute_declarative_module_stage,
)
from manyselves.core.reporting.models import REPORT_MODULE_IDS, ReportRequest
from manyselves.core.reporting.service import ReportingRunResult, ReportingService
from manyselves.core.reporting.taxonomy import REPORT_TAXONOMY
from manyselves.core.reporting.workflow import ReportWorkflowRunner
from manyselves.core.tools.task_board import TaskBoard
from manyselves.kernel.workflow import WorkflowStatus
from manyselves.runtime.state_store import FileWorkflowStateStore
from manyselves.runtime.workflow_host import FileWorkflowEventSink


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
    assert completed.outputs["result"]["run_id"] == "report-declarative-module"
    assert completed.subworkflow_states == {}
    assert [path.name for path in (tmp_path / "Work" / "runs").iterdir()] == [
        "report-declarative-module"
    ]


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


class _TopLevelTailRunner:
    def __init__(self, *, fail_stage: str | None = None) -> None:
        self.calls: list[str] = []
        self.fail_stage = fail_stage

    def _fail(self, stage: str) -> None:
        if self.fail_stage == stage:
            raise RuntimeError(f"injected {stage} failure")

    async def _cross_review(self, state: dict, workflow_id: str) -> None:
        self.calls.append("cross")
        self._fail("cross")
        state["cross_review_completion_ref"] = f"{workflow_id}/cross.json"

    async def _chief_edit(self, state: dict, workflow_id: str) -> None:
        self.calls.append("chief")
        self._fail("chief")
        state["chief_candidate_ref"] = f"{workflow_id}/chief.json"
        state["chief_editor_session_key"] = "chief-editor"
        state["approved_module_text"] = {"2.1": "approved"}

    async def _final_review_loop(self, state: dict, workflow_id: str, **kwargs) -> None:
        self.calls.append("final")
        self._fail("final")
        state["final_review_completion_ref"] = f"{workflow_id}/final.json"

    def _deliver(self, state: dict) -> None:
        self.calls.append("delivery")
        self._fail("delivery")
        state["delivery_completion_ref"] = "delivery.json"


@pytest.mark.asyncio
async def test_top_level_runtime_nests_the_file_defined_tail_in_one_run(
    tmp_path: Path,
) -> None:
    run_id = "report-declarative-one-state"
    state = {"run_id": run_id}
    tail = _TopLevelTailRunner()

    async def execute_current(_modules, current_state, _workflow_id) -> None:
        current_state["module_submissions"] = {
            module_id: ModuleSubmission(
                module_id=module_id,
                submodule_narratives={
                    submodule_id: f"{submodule_id} body"
                    for submodule_id in REPORT_TAXONOMY[module_id].submodules
                },
                claims=[],
                source_ids=[],
                unresolved_questions=[],
                revision=0,
            )
            for module_id in REPORT_MODULE_IDS
        }

    completed = await execute_declarative_module_stage(
        execute_current=execute_current,
        requested_modules=tuple(REPORT_MODULE_IDS),
        state=state,
        workflow_id=f"full-power-distribution-report:{run_id}",
        state_store=FileWorkflowStateStore(tmp_path),
        tail_runner=tail,
        event_sink=FileWorkflowEventSink(tmp_path),
    )

    assert tail.calls == ["cross", "chief", "final", "delivery"]
    assert state["delivery_completion_ref"] == "delivery.json"
    assert completed.status is WorkflowStatus.COMPLETED
    assert completed.subworkflow_states["run-reporting-tail"]["status"] == "completed"
    assert [path.name for path in (tmp_path / "Work" / "runs").iterdir()] == [run_id]
    events = [
        json.loads(line)
        for line in (
            tmp_path / "Work" / "runs" / run_id / "workflow-events.jsonl"
        ).read_text(encoding="utf-8").splitlines()
    ]
    assert [
        (event["kind"], event["action_id"])
        for event in events
        if event["workflow_id"] == "distribution-reporting-tail"
    ] == [
        ("workflow.started", None),
        ("action.started", "run-cross"),
        ("action.completed", "run-cross"),
        ("action.started", "run-chief"),
        ("action.completed", "run-chief"),
        ("action.started", "run-final"),
        ("action.completed", "run-final"),
        ("action.started", "run-delivery"),
        ("action.completed", "run-delivery"),
        ("action.started", "finish-reporting-tail"),
        ("action.completed", "finish-reporting-tail"),
        ("workflow.completed", None),
    ]


@pytest.mark.asyncio
async def test_top_level_runtime_resumes_inside_the_failed_tail_subworkflow(
    tmp_path: Path,
) -> None:
    run_id = "report-declarative-tail-resume"
    state = {"run_id": run_id}
    module_calls = 0

    async def execute_current(_modules, current_state, _workflow_id) -> None:
        nonlocal module_calls
        module_calls += 1
        current_state["module_submissions"] = {
            module_id: ModuleSubmission(
                module_id=module_id,
                submodule_narratives={
                    submodule_id: f"{submodule_id} body"
                    for submodule_id in REPORT_TAXONOMY[module_id].submodules
                },
                claims=[],
                source_ids=[],
                unresolved_questions=[],
                revision=0,
            )
            for module_id in REPORT_MODULE_IDS
        }

    store = FileWorkflowStateStore(tmp_path)
    failing_tail = _TopLevelTailRunner(fail_stage="chief")
    with pytest.raises(RuntimeError, match="injected chief failure"):
        await execute_declarative_module_stage(
            execute_current=execute_current,
            requested_modules=tuple(REPORT_MODULE_IDS),
            state=state,
            workflow_id=f"full-power-distribution-report:{run_id}",
            state_store=store,
            tail_runner=failing_tail,
        )

    failed = store.load(run_id)
    child = failed.subworkflow_states["run-reporting-tail"]
    assert child["actions"]["run-cross"]["status"] == "completed"
    assert child["actions"]["run-chief"]["status"] == "failed"

    resumed_tail = _TopLevelTailRunner()
    completed = await execute_declarative_module_stage(
        execute_current=execute_current,
        requested_modules=tuple(REPORT_MODULE_IDS),
        state=state,
        workflow_id=f"full-power-distribution-report:{run_id}",
        state_store=store,
        tail_runner=resumed_tail,
    )

    assert module_calls == 1
    assert failing_tail.calls == ["cross", "chief"]
    assert resumed_tail.calls == ["chief", "final", "delivery"]
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

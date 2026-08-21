from pathlib import Path

import pytest

from manyselves.core.reporting.agentic_models import ModuleSubmission
from manyselves.core.reporting.declarative_reporting_tail import (
    execute_declarative_reporting_tail,
)
from manyselves.core.reporting.taxonomy import REPORT_TAXONOMY
from manyselves.kernel.workflow import WorkflowStatus
from manyselves.runtime.semantic_trace import SemanticEventKind, SemanticTraceRecorder
from manyselves.runtime.state_store import FileWorkflowStateStore


class _TailRunner:
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
        assert kwargs["chief_session_key"] == "chief-editor"
        assert kwargs["approved_module_text"] == {"2.1": "approved"}
        assert kwargs["claims"] == []
        self._fail("final")
        state["final_review_completion_ref"] = f"{workflow_id}/final.json"

    def _deliver(self, state: dict) -> None:
        self.calls.append("delivery")
        self._fail("delivery")
        state["delivery_completion_ref"] = "delivery.json"
        state["delivery_status"] = "delivered"


def _state(run_id: str) -> dict:
    return {
        "run_id": run_id,
        "module_submissions": {
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
            for module_id in ("2.1", "2.2", "2.3", "2.4", "2.5")
        },
        "chief_editor_envelope": None,
    }


async def _capture_current_tail_trace(
    runner: _TailRunner,
    state: dict,
    workflow_id: str,
) -> list[dict[str, str]]:
    trace = SemanticTraceRecorder()
    trace.record(
        SemanticEventKind.WORKFLOW_STARTED,
        workflow_id=workflow_id,
        status="running",
    )
    stages = [
        ("cross", runner._cross_review),
        ("chief", runner._chief_edit),
        ("final", runner._final_review_loop),
        ("delivery", runner._deliver),
    ]
    for stage, invoke in stages:
        action_id = f"run-{stage}"
        tool_id = f"run-reporting-{stage}"
        trace.record(
            SemanticEventKind.ACTION_STARTED,
            workflow_id=workflow_id,
            action_id=action_id,
        )
        trace.record(
            SemanticEventKind.TOOL_INVOKED,
            workflow_id=workflow_id,
            action_id=action_id,
            tool_id=tool_id,
        )
        if stage in {"cross", "chief"}:
            await invoke(state, workflow_id)
        elif stage == "final":
            await invoke(
                state,
                workflow_id,
                chief_envelope=None,
                chief_session_key=state["chief_editor_session_key"],
                approved_module_text=state["approved_module_text"],
                claims=[],
            )
        else:
            invoke(state)
        trace.record(
            SemanticEventKind.ACTION_COMPLETED,
            workflow_id=workflow_id,
            action_id=action_id,
            status="completed",
        )
    trace.record(
        SemanticEventKind.OUTPUT_PUBLISHED,
        workflow_id=workflow_id,
        action_id="finish-reporting-tail",
        output_contract="reporting_tail_state",
        output_id=state["delivery_completion_ref"],
        status="completed",
    )
    trace.record(
        SemanticEventKind.WORKFLOW_COMPLETED,
        workflow_id=workflow_id,
        status="completed",
    )
    return trace.snapshot()


@pytest.mark.asyncio
async def test_declarative_reporting_tail_runs_current_stages_in_order(
    tmp_path: Path,
) -> None:
    runner = _TailRunner()
    state = _state("run-wp09-tail")
    store = FileWorkflowStateStore(tmp_path)

    completed = await execute_declarative_reporting_tail(
        runner=runner,
        state=state,
        workflow_id="workflow-wp09-tail",
        state_store=store,
    )

    assert runner.calls == ["cross", "chief", "final", "delivery"]
    assert state["delivery_status"] == "delivered"
    assert completed.status is WorkflowStatus.COMPLETED
    assert completed.outputs["result"]["delivery_completion_ref"] == "delivery.json"


@pytest.mark.asyncio
async def test_declarative_reporting_tail_matches_current_stage_trace(
    tmp_path: Path,
) -> None:
    workflow_id = "workflow-wp09-trace"
    trace = SemanticTraceRecorder()
    declarative_runner = _TailRunner()

    await execute_declarative_reporting_tail(
        runner=declarative_runner,
        state=_state("run-wp09-declarative-trace"),
        workflow_id=workflow_id,
        state_store=FileWorkflowStateStore(tmp_path / "declarative"),
        trace=trace,
    )
    current = await _capture_current_tail_trace(
        _TailRunner(),
        _state("run-wp09-current-trace"),
        workflow_id,
    )

    assert trace.snapshot() == current


@pytest.mark.asyncio
async def test_declarative_reporting_tail_skips_current_completion_markers(
    tmp_path: Path,
) -> None:
    runner = _TailRunner()
    state = _state("run-wp09-resume")
    state.update(
        {
            "cross_review_completion_ref": "existing-cross.json",
            "chief_candidate_ref": "existing-chief.json",
            "chief_editor_session_key": "chief-editor",
            "approved_module_text": {"2.1": "approved"},
        }
    )

    await execute_declarative_reporting_tail(
        runner=runner,
        state=state,
        workflow_id="workflow-wp09-resume",
        state_store=FileWorkflowStateStore(tmp_path),
    )

    assert runner.calls == ["final", "delivery"]
    assert state["cross_review_completion_ref"] == "existing-cross.json"
    assert state["chief_candidate_ref"] == "existing-chief.json"


@pytest.mark.asyncio
async def test_declarative_reporting_tail_resumes_failed_stage_from_saved_state(
    tmp_path: Path,
) -> None:
    run_id = "run-wp09-failed-tail"
    state = _state(run_id)
    store = FileWorkflowStateStore(tmp_path)
    failing = _TailRunner(fail_stage="chief")

    with pytest.raises(RuntimeError, match="injected chief failure"):
        await execute_declarative_reporting_tail(
            runner=failing,
            state=state,
            workflow_id="workflow-wp09-failed-tail",
            state_store=store,
        )

    assert failing.calls == ["cross", "chief"]
    assert state["cross_review_completion_ref"].endswith("/cross.json")
    saved = store.load(run_id)
    assert saved.status is WorkflowStatus.FAILED

    resumed = _TailRunner()
    completed = await execute_declarative_reporting_tail(
        runner=resumed,
        state=state,
        workflow_id="workflow-wp09-failed-tail",
        state_store=store,
    )

    assert resumed.calls == ["chief", "final", "delivery"]
    assert completed.status is WorkflowStatus.COMPLETED
    assert [path.name for path in (tmp_path / "Work" / "runs").iterdir()] == [
        run_id
    ]

from pathlib import Path

import pytest

from manyselves.capabilities.distribution_reporting.domain.taxonomy import REPORT_TAXONOMY
from manyselves.capabilities.distribution_reporting.runtime.models.agentic import (
    EditedReportSubmission,
    ModuleSubmission,
)
from manyselves.capabilities.distribution_reporting.runtime.models.delivery import (
    DeliveryContext,
)
from manyselves.core.reporting.declarative_reporting_tail import (
    execute_declarative_reporting_tail,
)
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

    def _prepare_and_render_delivery(self, state: dict) -> DeliveryContext:
        self.calls.append("prepare")
        self._fail("prepare")
        return _delivery_context(state)

    def _prepare_state(self, state: dict) -> dict:
        self.calls.append("prepare")
        self._fail("prepare")
        context = _delivery_context(state)
        state["_declarative_delivery_context"] = context.model_dump(
            mode="json", exclude={"state"}
        )
        return state

    def _publish_and_materialize_delivery(
        self,
        context: DeliveryContext,
    ) -> DeliveryContext:
        self.calls.append("publish")
        self._fail("publish")
        return context

    def _publish_state(self, state: dict) -> dict:
        self.calls.append("publish")
        self._fail("publish")
        return state

    def _complete_delivery(self, context: DeliveryContext) -> None:
        self.calls.append("complete")
        self._fail("complete")
        context.state["delivery_completion_ref"] = "delivery.json"
        context.state["delivery_status"] = "delivered"


def _delivery_context(state: dict) -> DeliveryContext:
    root = Path("Work") / "runs" / str(state["run_id"])
    return DeliveryContext(
        state=state,
        final_audit_snapshot_ref=f"{root}/final-audit.json",
        claim_ledger_path=root / "claims.json",
        source_ledger_path=root / "sources.json",
        evidence_snapshot_path=root / "evidence.jsonl",
        approved_module_paths={},
        edited_submission_path=root / "edited.json",
        request_snapshot_path=root / "request.json",
        photo_manifest_path=root / "photos.json",
        delivery_markdown="# Report",
        report_state_path=root / "report-state.json",
        markdown_path=root / "report.md",
        source_index_markdown="# Sources",
        source_index_path=root / "sources.md",
        source_index_docx_path=root / "sources.docx",
        template_snapshot=root / "template.docx",
        template_provenance_path=root / "template.json",
        output=root / "report.docx",
        render_result_ref=root / "render-result.json",
    )


def _state(run_id: str) -> dict:
    return {
        "run_id": run_id,
        "edited_report": EditedReportSubmission(
            title="Report",
            assessment_background="background",
            findings_overview="overview",
            regional_executive_summary="summary",
            module_narratives={
                module_id: f"module {module_id}"
                for module_id in REPORT_TAXONOMY
            },
            risk_panorama="panorama",
            dimension_risk_analysis="risk analysis",
            data_gap_analysis="gaps",
            improvement_action_plan="actions",
        ),
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
        ("cross", runner._cross_review, True),
        ("chief", runner._chief_edit, True),
    ]
    for stage, invoke, is_async in stages:
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
        if is_async:
            await invoke(state, workflow_id)
        trace.record(
            SemanticEventKind.ACTION_COMPLETED,
            workflow_id=workflow_id,
            action_id=action_id,
            status="completed",
        )
    delivery_context = _delivery_context(state)
    for action_id, tool_id, invoke in [
        (
            "prepare-render-delivery",
            "prepare-render-delivery",
            lambda: runner._prepare_and_render_delivery(state),
        ),
        (
            "publish-materialize-delivery",
            "publish-materialize-delivery",
            lambda: runner._publish_and_materialize_delivery(delivery_context),
        ),
        (
            "complete-delivery",
            "complete-delivery",
            lambda: runner._complete_delivery(delivery_context),
        ),
    ]:
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
        result = invoke()
        if isinstance(result, DeliveryContext):
            delivery_context = result
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
        prepare_tool=runner._prepare_state,
        publish_tool=runner._publish_state,
    )

    assert runner.calls == ["cross", "chief", "prepare", "publish", "complete"]
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
        prepare_tool=declarative_runner._prepare_state,
        publish_tool=declarative_runner._publish_state,
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
        prepare_tool=runner._prepare_state,
        publish_tool=runner._publish_state,
    )

    assert runner.calls == ["prepare", "publish", "complete"]
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
            prepare_tool=failing._prepare_state,
            publish_tool=failing._publish_state,
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
        prepare_tool=resumed._prepare_state,
        publish_tool=resumed._publish_state,
    )

    assert resumed.calls == ["chief", "prepare", "publish", "complete"]
    assert completed.status is WorkflowStatus.COMPLETED
    assert [path.name for path in (tmp_path / "Work" / "runs").iterdir()] == [
        run_id
    ]


@pytest.mark.asyncio
async def test_declarative_reporting_tail_resumes_failed_publish_without_replaying_prepare(
    tmp_path: Path,
) -> None:
    run_id = "run-wp09-failed-delivery-publish"
    state = _state(run_id)
    store = FileWorkflowStateStore(tmp_path)
    failing = _TailRunner(fail_stage="publish")

    with pytest.raises(RuntimeError, match="injected publish failure"):
        await execute_declarative_reporting_tail(
            runner=failing,
            state=state,
            workflow_id="workflow-wp09-failed-delivery-publish",
            state_store=store,
            prepare_tool=failing._prepare_state,
            publish_tool=failing._publish_state,
        )

    assert failing.calls == ["cross", "chief", "prepare", "publish"]
    saved = store.load(run_id)
    delivery_state = saved.subworkflow_states["run-delivery"]
    assert delivery_state["actions"]["prepare-render-delivery"]["status"] == "completed"
    assert delivery_state["actions"]["publish-materialize-delivery"]["status"] == "failed"

    resumed = _TailRunner()
    completed = await execute_declarative_reporting_tail(
        runner=resumed,
        state=state,
        workflow_id="workflow-wp09-failed-delivery-publish",
        state_store=store,
        prepare_tool=resumed._prepare_state,
        publish_tool=resumed._publish_state,
    )

    assert resumed.calls == ["publish", "complete"]
    assert completed.status is WorkflowStatus.COMPLETED
    assert state["delivery_completion_ref"] == "delivery.json"

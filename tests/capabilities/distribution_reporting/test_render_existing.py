"""Focused characterization for the file-defined render-existing entrypoint."""

import json
from pathlib import Path
from uuid import UUID

import pytest

from manyselves.capabilities.distribution_reporting import (
    load_distribution_reporting_capability,
)
from manyselves.capabilities.distribution_reporting.adapters.runtime import (
    DistributionReportingRuntimeBinding,
)
from manyselves.capabilities.distribution_reporting.runtime.render_existing import (
    RenderExistingWorkflowRuntime,
)
from manyselves.config.schema import AgentDefaults
from manyselves.core.loops.bus import MessageBus
from manyselves.kernel.definitions import DefinitionKind
from manyselves.kernel.workflow import ResolvedPlan, WorkflowState, WorkflowStatus
from manyselves.runtime.services import RuntimeServicesView
from manyselves.runtime.state_store import FileWorkflowStateStore


def test_render_existing_is_declared_as_a_file_workflow_entrypoint() -> None:
    capability, registry = load_distribution_reporting_capability()

    assert "render-existing" in capability.entrypoints
    workflow = registry.require(DefinitionKind.WORKFLOW, "render-existing")
    assert workflow.id == "render-existing"


@pytest.mark.asyncio
async def test_render_existing_starts_through_the_generic_host(
    tmp_path: Path,
) -> None:
    source = tmp_path / "Inputs" / "approved.md"
    source.parent.mkdir(parents=True)
    source.write_text("# Approved\n\nExisting approved prose.\n", encoding="utf-8")

    binding = DistributionReportingRuntimeBinding(
        tmp_path,
        RuntimeServicesView(
            workspace=tmp_path,
            bus=MessageBus(),
            active_provider=None,
            agent_defaults=AgentDefaults(),
            global_knowledge_root=None,
        ),
    )
    started = await binding.start(
        UUID("50000000-0000-4000-8000-000000000001"),
        "render-existing",
        {
            "operation": "render_existing",
            "instruction": "Render the approved Markdown as DOCX.",
            "source_markdown_ref": "Inputs/approved.md",
            "output_filename": "approved.docx",
        },
    )

    run_id = started["run_id"]
    run_root = tmp_path / "Work" / "runs" / run_id
    event_path = run_root / "workflow-events.jsonl"
    expected_files = (
        "input-snapshot.json",
        "resolved-plan.json",
        "runtime-state.json",
        "render-request.json",
        "template-provenance.json",
        "render-log.json",
        "render-result.json",
        "templates/report_template.docx",
    )
    assert all((run_root / relative).is_file() for relative in expected_files)
    assert binding.get_run(run_id)["run"]["status"] == "completed"
    outputs = binding.get_outputs(run_id)["outputs"]
    assert outputs[0]["id"] == "result"
    assert outputs[0]["value"]["output_ref"] == "Outputs/Reports/approved.docx"
    assert outputs[1] == {
        "id": "Outputs/Reports/approved.docx",
        "kind": "artifact",
        "path": "Outputs/Reports/approved.docx",
        "exists": True,
        "size": (tmp_path / "Outputs/Reports/approved.docx").stat().st_size,
    }
    render_request = json.loads(
        (run_root / "render-request.json").read_text(encoding="utf-8")
    )
    assert render_request["source_snapshot_ref"] == (
        f"Work/runs/{run_id}/frozen-project/Inputs/approved.md"
    )
    provenance = json.loads(
        (run_root / "template-provenance.json").read_text(encoding="utf-8")
    )
    assert provenance["source"] == "packaged"
    assert provenance["selected_path"] == (
        "manyselves/templates/reporting/report_template.docx"
    )
    assert provenance["snapshot_path"] == "Work/runs/" + run_id + "/templates/report_template.docx"
    events = [
        json.loads(line)
        for line in event_path.read_text(encoding="utf-8").splitlines()
    ]
    assert events[0]["kind"] == "workflow.started"
    prepare_started = next(
        index
        for index, event in enumerate(events)
        if event["kind"] == "action.started"
        and event["action_id"] == "prepare-render"
    )
    completed = next(
        index for index, event in enumerate(events) if event["kind"] == "workflow.completed"
    )
    assert prepare_started < completed


@pytest.mark.asyncio
async def test_render_existing_resume_loads_saved_plan_and_state(
    tmp_path: Path,
) -> None:
    runtime = RenderExistingWorkflowRuntime(tmp_path)
    run_id = "render-existing-restart"
    plan = ResolvedPlan(
        workflow_id="render-existing",
        workflow_version="1.0.0",
        actions=[],
    )
    state = WorkflowState.for_plan(run_id, plan)
    state.status = WorkflowStatus.RUNNING
    FileWorkflowStateStore(tmp_path).save_plan(run_id, plan)
    FileWorkflowStateStore(tmp_path).save(state)
    observed: dict[str, object] = {}

    async def execute(
        saved_plan: object,
        saved_state: object,
        registry: object,
        contracts: object,
    ) -> WorkflowState:
        observed.update(
            {
                "plan": saved_plan,
                "state": saved_state,
                "registry": registry,
                "contracts": contracts,
            }
        )
        return state

    runtime._execute = execute

    accepted = await runtime.resume(
        UUID("50000000-0000-4000-8000-000000000009"),
        run_id,
    )

    assert accepted == {"run_id": run_id, "task_id": None}
    assert observed["plan"] == plan
    assert observed["state"] == state


@pytest.mark.asyncio
async def test_render_existing_resume_is_idempotent_for_completed_and_retries_failed(
    tmp_path: Path,
) -> None:
    runtime = RenderExistingWorkflowRuntime(tmp_path)
    plan = ResolvedPlan(
        workflow_id="render-existing",
        workflow_version="1.0.0",
        actions=[],
    )
    store = FileWorkflowStateStore(tmp_path)
    completed = WorkflowState.for_plan("render-existing-completed", plan)
    completed.status = WorkflowStatus.COMPLETED
    failed = WorkflowState.for_plan("render-existing-failed", plan)
    failed.status = WorkflowStatus.FAILED
    for run_id, state in (
        (completed.run_id, completed),
        (failed.run_id, failed),
    ):
        store.save_plan(run_id, plan)
        store.save(state)

    observed: list[WorkflowStatus] = []

    async def execute(plan, state, registry, contracts):
        del plan, registry, contracts
        observed.append(state.status)
        return state

    runtime._execute = execute

    assert await runtime.resume(
        UUID("50000000-0000-4000-8000-000000000010"),
        completed.run_id,
    ) == {"run_id": completed.run_id, "task_id": None}
    assert await runtime.resume(
        UUID("50000000-0000-4000-8000-000000000011"),
        failed.run_id,
    ) == {"run_id": failed.run_id, "task_id": None}
    assert observed == [WorkflowStatus.FAILED]

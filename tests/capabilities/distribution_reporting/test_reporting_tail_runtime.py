"""Characterization for the Capability-owned full-report tail composition."""

from pathlib import Path

import pytest


def test_full_report_tail_composition_reaches_first_unbound_cross_tool(
    tmp_path: Path,
) -> None:
    """The public full-report root needs a Capability tail composition."""

    from manyselves.capabilities.distribution_reporting.runtime.reporting_tail_runtime import (
        ReportingTailComposition,
    )

    assert ReportingTailComposition(tmp_path).workflow_specializer is not None


@pytest.mark.asyncio
async def test_full_report_tail_host_stops_at_first_unbound_cross_tool(
    tmp_path: Path,
) -> None:
    from manyselves.capabilities.distribution_reporting.runtime.models.reporting import (
        ReportRequest,
    )
    from manyselves.capabilities.distribution_reporting.runtime.public_reporting import (
        PublicReportingWorkflowRuntime,
    )
    from manyselves.capabilities.distribution_reporting.runtime.reporting_tail_runtime import (
        ReportingTailComposition,
    )
    from manyselves.kernel.conversations import ConversationRegistry
    from manyselves.kernel.executors import RuntimeContext
    from manyselves.kernel.workflow import WorkflowState, WorkflowStatus
    from manyselves.runtime.conversation_store import FileConversationStore
    from manyselves.runtime.state_store import InMemoryWorkflowStateStore
    from manyselves.runtime.workflow_host import (
        InMemoryWorkflowEventSink,
        WorkflowRuntimeHost,
    )

    store = InMemoryWorkflowStateStore()
    events = InMemoryWorkflowEventSink()
    composition = ReportingTailComposition(tmp_path)
    runtime = PublicReportingWorkflowRuntime(
        tmp_path,
        input_snapshot=lambda _run_id: {},
        snapshot_content=lambda source, target: (target, "a" * 64, "blob"),
        runtime_photo_ids=lambda _evidence, _photos: None,
        workflow_specializers=(composition.workflow_specializer,),
        additional_tool_implementations=composition.tool_implementations(),
        state_store=store,
        events=events,
    )
    request = ReportRequest(
        operation="full_report",
        instruction="run the full report tail",
        target_modules=["2.1", "2.2", "2.3", "2.4", "2.5"],
        missing_evidence_policy="draft",
        preparation_mode="serial",
    )

    definitions, contracts, root_plan = runtime.compile_plan(request)
    tail_plan = root_plan.subworkflow_plans["distribution-reporting-tail"]
    tools = runtime._tools(definitions, contracts, root_plan)
    assert tail_plan.workflow_id == "distribution-reporting-tail"
    cross_plan = root_plan.subworkflow_plans["distribution-cross-owner-cohort"]
    assert "distribution-cross-owner-2.1-pipeline" in cross_plan.workflow_ids
    assert "prepare-render-delivery" in tools
    assert "complete-delivery" in tools
    assert "prepare-cross-owner-cohort" not in tools

    run_id = "public-full-tail-boundary"
    state = WorkflowState.for_plan(
        run_id,
        tail_plan,
        initial_variables={"reporting-state": {"run_id": run_id}},
    )
    host = WorkflowRuntimeHost(runtime.executors, store, events)

    with pytest.raises(RuntimeError, match="missing tool adapter: prepare-cross-owner-cohort"):
        await host.execute(
            tail_plan,
            state,
            RuntimeContext(
                tools=tools,
                contracts=contracts,
                definitions=definitions,
                conversations=ConversationRegistry(FileConversationStore(tmp_path)),
                subworkflows=root_plan.subworkflow_plans,
            ),
        )

    persisted = store.load(run_id)
    assert persisted.status is WorkflowStatus.FAILED
    assert persisted.actions["run-cross"].status.value == "failed"
    assert "missing tool adapter: prepare-cross-owner-cohort" in (
        persisted.actions["run-cross"].error or ""
    )

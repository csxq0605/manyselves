"""Characterization for the Capability-owned full-report tail composition."""

import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


def _cross_port_with_all_declared_tools() -> SimpleNamespace:
    attributes = {
        "prepare",
        "reduce",
        "prepare_initial",
        "initial_requires_agent",
        "accept_initial",
        "initial_has_findings",
        "prepare_revision",
        "revision_requires_agent",
        "accept_revision",
        "prepare_author_exception",
        "prepare_reviewer_exception",
        "main_exception_requires_agent",
        "accept_main_exception",
        "main_exception_requests_user",
        "apply_main_exception_user_input",
        "author_exception_returns_to_author",
        "prepare_local_review",
        "local_review_requires_agent",
        "accept_local_review",
        "prepare_recheck",
        "recheck_requires_agent",
        "accept_recheck",
        "advance_round",
        "round_needs_revision",
        "complete_owner_round",
        "complete_owner_without_findings",
    }
    return SimpleNamespace(
        agent_invokers={"cross-module-reviewer": object()},
        **{attribute: (lambda value=None: value) for attribute in attributes},
    )


def test_full_report_tail_composition_reaches_first_unbound_cross_tool(
    tmp_path: Path,
) -> None:
    """The public full-report root needs a Capability tail composition."""

    from manyselves.capabilities.distribution_reporting.runtime.reporting_tail_runtime import (
        ReportingTailComposition,
    )

    assert ReportingTailComposition(tmp_path).workflow_specializer is not None


def test_full_report_tail_composition_binds_declared_cross_ports(
    tmp_path: Path,
) -> None:
    """An injected lifecycle supplies all declared Cross actions by name."""

    from manyselves.capabilities.distribution_reporting.runtime.reporting_tail_runtime import (
        ReportingTailComposition,
    )

    cross_runtime = _cross_port_with_all_declared_tools()
    composition = ReportingTailComposition(tmp_path, cross_runtime=cross_runtime)
    tools = composition.tool_implementations()

    assert "prepare-cross-owner-cohort" in tools
    assert "reduce-cross-owner-cohort" in tools
    assert "prepare-current-cross-owner-initial" in tools
    assert "prepare-current-cross-owner-local-review" in tools
    assert "prepare-current-cross-owner-recheck" in tools
    assert "complete-current-cross-owner-pipeline" in tools
    assert composition.agent_invokers == cross_runtime.agent_invokers


def test_cross_composition_import_does_not_load_core_reporting() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import json, sys\n"
                "import manyselves.capabilities.distribution_reporting.runtime.cross_owner_composition\n"
                "print(json.dumps(sorted(name for name in sys.modules if name.startswith('manyselves.core.reporting'))))"
            ),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    assert completed.stdout.strip() == "[]"


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

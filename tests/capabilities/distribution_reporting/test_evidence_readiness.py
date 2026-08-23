"""Characterization for the file-defined Distribution evidence readiness flow."""

from __future__ import annotations

from pathlib import Path

import pytest

from manyselves.capabilities.distribution_reporting import (
    load_distribution_reporting_capability,
)
from manyselves.capabilities.distribution_reporting.domain.coverage import (
    evaluate_coverage,
)
from manyselves.capabilities.distribution_reporting.runtime.models.reporting import (
    ReportRequest,
)
from manyselves.kernel.definitions import DefinitionKind, WorkflowDefinition
from manyselves.kernel.executors import build_builtin_executor_registry
from manyselves.kernel.workflow import ActionKind, WorkflowCompiler


def _request(policy: str) -> ReportRequest:
    return ReportRequest(
        operation="full_report",
        instruction="characterize evidence readiness",
        missing_evidence_policy=policy,
    )


def test_capability_readiness_policy_exposes_all_missing_evidence_actions() -> None:
    from manyselves.capabilities.distribution_reporting.domain.evidence_readiness import (
        evaluate_evidence_readiness,
    )
    from manyselves.capabilities.distribution_reporting.runtime.models.evidence_readiness import (
        EvidenceReadinessInput,
    )

    outcomes = {}
    for policy in ("ask", "block", "draft", "skip"):
        request = _request(policy)
        context = EvidenceReadinessInput(
            run_id=f"readiness-{policy}",
            request=request,
            coverage_matrix=evaluate_coverage(request, []),
        )
        outcomes[policy] = evaluate_evidence_readiness(context).status

    assert outcomes == {
        "ask": "waiting",
        "block": "blocked",
        "draft": "draft",
        "skip": "skipped",
    }


def test_readiness_workflow_is_file_defined_and_uses_generic_actions() -> None:
    _capability, registry = load_distribution_reporting_capability()
    workflow = registry.require(
        DefinitionKind.WORKFLOW,
        "distribution-evidence-readiness",
    )
    assert isinstance(workflow, WorkflowDefinition)

    kinds = {action["kind"] for action in workflow.actions}
    assert {
        ActionKind.INVOKE_TOOL,
        ActionKind.IF,
        ActionKind.REQUEST_INPUT,
        ActionKind.PUBLISH_RESULT,
        ActionKind.FAIL_WORKFLOW,
        ActionKind.END_WORKFLOW,
    } <= kinds

    plan = WorkflowCompiler(build_builtin_executor_registry()).compile(
        workflow,
        registry,
    )
    assert plan.workflow_id == "distribution-evidence-readiness"
    assert plan.agent_ids == []
    assert plan.task_ids == []


@pytest.mark.asyncio
@pytest.mark.parametrize("policy", ["ask", "block", "draft", "skip"])
async def test_readiness_runtime_maps_policy_to_generic_wait_or_terminal_route(
    policy: str,
    tmp_path: Path,
) -> None:
    """The Host must expose WAITING for ask and use generic completion/failure paths."""

    from manyselves.capabilities.distribution_reporting.runtime.evidence_readiness import (
        build_evidence_readiness_tool_implementations,
    )
    from manyselves.capabilities.distribution_reporting.runtime.models.preparation import (
        PreparationContext,
    )
    from manyselves.kernel.contracts import build_contract_catalog
    from manyselves.kernel.executors import RuntimeContext, RuntimeExecutionError
    from manyselves.kernel.workflow import WorkflowState, WorkflowStatus
    from manyselves.runtime.state_store import InMemoryWorkflowStateStore
    from manyselves.runtime.tool_adapter import CapabilityToolAdapterFactory
    from manyselves.runtime.workflow_host import (
        InMemoryWorkflowEventSink,
        WorkflowRuntimeHost,
    )

    _capability, registry = load_distribution_reporting_capability()
    workflow = registry.require(
        DefinitionKind.WORKFLOW,
        "distribution-evidence-readiness",
    )
    assert isinstance(workflow, WorkflowDefinition)
    contracts = build_contract_catalog(registry)
    plan = WorkflowCompiler(build_builtin_executor_registry()).compile(
        workflow,
        registry,
    )
    implementations = build_evidence_readiness_tool_implementations()
    factory = CapabilityToolAdapterFactory(
        "distribution-reporting",
        implementations,
        contracts,
    )
    tools = {
        tool_id: factory.build(registry.require(DefinitionKind.TOOL, tool_id))
        for tool_id in plan.tool_ids
    }
    request = _request(policy)
    context = PreparationContext(
        run_id=f"host-{policy}",
        request=request,
        coverage_matrix=evaluate_coverage(request, []),
    )
    store = InMemoryWorkflowStateStore()
    host = WorkflowRuntimeHost(
        build_builtin_executor_registry(),
        store,
        InMemoryWorkflowEventSink(),
    )
    initial = WorkflowState.for_plan(
        f"host-{policy}",
        plan,
        initial_variables={"preparation-context": context},
    )
    try:
        state = await host.execute(
            plan,
            initial,
            RuntimeContext(tools=tools, contracts=contracts, definitions=registry),
        )
    except RuntimeExecutionError:
        state = store.load(f"host-{policy}")

    if policy == "ask":
        assert state.status is WorkflowStatus.WAITING
        assert state.waiting_input is not None
        assert state.waiting_input["interaction_id"] == "evidence-readiness-decision"
        assert state.outputs["readiness"].status == "waiting"
        assert state.outputs["readiness"].missing_evidence
        assert state.outputs["readiness"].affected_modules
    elif policy == "block":
        assert state.status is WorkflowStatus.FAILED
    else:
        assert state.status is WorkflowStatus.COMPLETED
        assert state.outputs["result"].status == {
            "draft": "draft",
            "skip": "skipped",
        }[policy]


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["draft", "skip"])
async def test_ask_waiting_input_resumes_same_run_to_draft_or_skip(
    decision: str,
) -> None:
    from manyselves.capabilities.distribution_reporting.runtime.evidence_readiness import (
        build_evidence_readiness_tool_implementations,
    )
    from manyselves.capabilities.distribution_reporting.runtime.models.preparation import (
        PreparationContext,
    )
    from manyselves.kernel.contracts import build_contract_catalog
    from manyselves.kernel.executors import RuntimeContext
    from manyselves.kernel.workflow import (
        WorkflowState,
        WorkflowStatus,
        resume_waiting_input,
    )
    from manyselves.runtime.state_store import InMemoryWorkflowStateStore
    from manyselves.runtime.tool_adapter import CapabilityToolAdapterFactory
    from manyselves.runtime.workflow_host import (
        InMemoryWorkflowEventSink,
        WorkflowRuntimeHost,
    )

    _capability, registry = load_distribution_reporting_capability()
    workflow = registry.require(
        DefinitionKind.WORKFLOW,
        "distribution-evidence-readiness",
    )
    assert isinstance(workflow, WorkflowDefinition)
    contracts = build_contract_catalog(registry)
    plan = WorkflowCompiler(build_builtin_executor_registry()).compile(
        workflow,
        registry,
    )
    factory = CapabilityToolAdapterFactory(
        "distribution-reporting",
        build_evidence_readiness_tool_implementations(),
        contracts,
    )
    tools = {
        tool_id: factory.build(registry.require(DefinitionKind.TOOL, tool_id))
        for tool_id in plan.tool_ids
    }
    run_id = f"ask-resume-{decision}"
    request = _request("ask")
    input_value = PreparationContext(
        run_id=run_id,
        request=request,
        coverage_matrix=evaluate_coverage(request, []),
    )
    store = InMemoryWorkflowStateStore()
    host = WorkflowRuntimeHost(
        build_builtin_executor_registry(),
        store,
        InMemoryWorkflowEventSink(),
    )
    context = RuntimeContext(tools=tools, contracts=contracts, definitions=registry)
    waiting = await host.execute(
        plan,
        WorkflowState.for_plan(
            run_id,
            plan,
            initial_variables={"preparation-context": input_value},
        ),
        context,
    )
    assert waiting.status is WorkflowStatus.WAITING
    resumed = resume_waiting_input(
        plan,
        waiting,
        input_id="request-evidence-decision",
        values={"action": decision},
        contracts=contracts,
    )

    completed = await host.execute(plan, resumed, context)

    assert completed.run_id == run_id
    assert completed.status is WorkflowStatus.COMPLETED
    assert completed.waiting_input is None
    assert completed.outputs["result"].status == {
        "draft": "draft",
        "skip": "skipped",
    }[decision]
    assert completed.outputs["result"].selected_action == decision


def _ready_coverage(request: ReportRequest):
    from manyselves.capabilities.distribution_reporting.runtime.models.reporting import (
        CoverageStatus,
    )

    current = evaluate_coverage(request, [])
    return current.model_copy(
        update={
            "entries": {
                module_id: entry.model_copy(
                    update={
                        "status": CoverageStatus.READY,
                        "gaps": [],
                        "submodules": {
                            submodule_id: submodule.model_copy(
                                update={
                                    "status": CoverageStatus.READY,
                                    "gaps": [],
                                }
                            )
                            for submodule_id, submodule in entry.submodules.items()
                        },
                    }
                )
                for module_id, entry in current.entries.items()
            }
        }
    )


@pytest.mark.asyncio
async def test_supplement_waiting_resumes_through_preparation_subworkflow(
) -> None:
    """A supplement must rerun the existing preparation workflow in this Run."""

    from manyselves.capabilities.distribution_reporting.runtime.evidence_readiness import (
        build_evidence_readiness_tool_implementations,
    )
    from manyselves.capabilities.distribution_reporting.runtime.models.preparation import (
        PreparationContext,
    )
    from manyselves.capabilities.distribution_reporting.runtime.models.reporting import (
        UserSupplement,
    )
    from manyselves.kernel.contracts import build_contract_catalog
    from manyselves.kernel.executors import RuntimeContext
    from manyselves.kernel.workflow import (
        SubworkflowAction,
        WorkflowState,
        WorkflowStatus,
        resume_waiting_input,
    )
    from manyselves.runtime.state_store import InMemoryWorkflowStateStore
    from manyselves.runtime.tool_adapter import CapabilityToolAdapterFactory
    from manyselves.runtime.workflow_host import (
        InMemoryWorkflowEventSink,
        WorkflowRuntimeHost,
    )

    _capability, registry = load_distribution_reporting_capability()
    workflow = registry.require(
        DefinitionKind.WORKFLOW,
        "distribution-evidence-readiness",
    )
    assert isinstance(workflow, WorkflowDefinition)
    plan = WorkflowCompiler(build_builtin_executor_registry()).compile(
        workflow,
        registry,
    )
    preparation_actions = [
        action
        for action in plan.actions
        if isinstance(action, SubworkflowAction)
        and action.workflow == "distribution-reporting-preparation"
    ]
    assert preparation_actions

    request = _request("ask")
    run_id = "readiness-supplement-run"
    prepared = PreparationContext(
        run_id=run_id,
        request=request,
        coverage_matrix=evaluate_coverage(request, []),
    )
    tool_calls: list[str] = []
    provider_calls: list[str] = []

    def preparation_tool(tool_id: str):
        def invoke(arguments: object) -> PreparationContext:
            tool_calls.append(tool_id)
            context = PreparationContext.model_validate(arguments)
            return context.model_copy(
                update={"coverage_matrix": _ready_coverage(context.request)}
            )

        return invoke

    readiness_contracts = build_contract_catalog(registry)
    readiness_factory = CapabilityToolAdapterFactory(
        "distribution-reporting",
        build_evidence_readiness_tool_implementations(),
        readiness_contracts,
    )
    tools = {
        tool_id: readiness_factory.build(registry.require(DefinitionKind.TOOL, tool_id))
        for tool_id in plan.tool_ids
        if tool_id in {
            "evaluate-evidence-readiness",
            "route-evidence-readiness",
            "apply-evidence-decision",
            "prepare-evidence-supplement",
        }
    }
    preparation_plan = plan.subworkflow_plans["distribution-reporting-preparation"]
    tools.update(
        {
            tool_id: preparation_tool(tool_id)
            for tool_id in preparation_plan.tool_ids
            if tool_id != "restore-preparation-snapshot"
        }
    )
    host = WorkflowRuntimeHost(
        build_builtin_executor_registry(),
        InMemoryWorkflowStateStore(),
        InMemoryWorkflowEventSink(),
    )
    context = RuntimeContext(
        tools=tools,
        contracts=readiness_contracts,
        definitions=registry,
        agents={"provider-spy": lambda *_args, **_kwargs: provider_calls.append("agent")},
    )
    waiting = await host.execute(
        plan,
        WorkflowState.for_plan(
            run_id,
            plan,
            initial_variables={"preparation-context": prepared},
        ),
        context,
    )
    assert waiting.status is WorkflowStatus.WAITING
    resumed = resume_waiting_input(
        plan,
        waiting,
        input_id="request-evidence-decision",
        values={
            "action": "supplement",
            "supplements": [UserSupplement(content="new evidence")],
        },
        contracts=readiness_contracts,
    )

    completed = await host.execute(plan, resumed, context)

    assert completed.run_id == run_id
    assert completed.status is WorkflowStatus.COMPLETED
    assert completed.outputs["result"].status == "ready"
    assert tool_calls
    assert "evaluate-coverage" in tool_calls
    assert provider_calls == []
    assert plan.agent_ids == []

"""Reporting-owned declarative cohort for the five module lanes."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from manyselves.capabilities.distribution_reporting import (
    load_distribution_reporting_capability,
)
from manyselves.kernel.contracts import ContractAdapter, build_contract_catalog
from manyselves.kernel.definitions import (
    DefinitionKind,
    DefinitionRegistry,
    WorkflowDefinition,
    specialize_workflow,
)
from manyselves.kernel.executors import RuntimeContext, build_builtin_executor_registry
from manyselves.kernel.ports import AgentInvoker, WorkflowStateStore
from manyselves.kernel.workflow import (
    ResolvedPlan,
    WorkflowCompiler,
    WorkflowState,
    WorkflowStatus,
    retry_parallel_branches,
)
from manyselves.runtime.state_store import InMemoryWorkflowStateStore
from manyselves.runtime.workflow_host import (
    InMemoryWorkflowEventSink,
    WorkflowRuntimeHost,
)

from .agentic_models import ModuleSubmission
from .declarative_module_lane import (
    ModuleSubjectValidator,
    execute_declarative_module_lane,
)
from .taxonomy import REPORT_TAXONOMY


class DeclarativeModuleLaneOutcome(BaseModel):
    """Business outcome joined after every sibling branch has drained."""

    model_config = ConfigDict(extra="forbid")

    module_id: str
    status: Literal["completed", "deferred", "failed"]
    module: ModuleSubmission | None = None
    error: str | None = None
    lane_state: dict[str, Any] | None = None
    completion_ref: str | None = None
    completion: dict[str, Any] | None = None


class DeclarativeModuleCohortError(RuntimeError):
    """Raised after the joined cohort contains one or more failed lanes."""


def build_module_cohort_definition(
    *,
    max_concurrency: int | None,
) -> tuple[DefinitionRegistry, dict[str, ContractAdapter], WorkflowDefinition]:
    """Specialize the packaged fixed five-branch Reporting workflow."""

    _capability, registry = load_distribution_reporting_capability()
    template = registry.require(
        DefinitionKind.WORKFLOW,
        "distribution-module-cohort",
    )
    if not isinstance(template, WorkflowDefinition):
        raise TypeError("distribution-module-cohort is not a workflow")
    workflow = specialize_workflow(
        template,
        {"max_concurrency": max_concurrency},
    )
    return registry, build_contract_catalog(registry), workflow


async def execute_declarative_module_cohort(
    *,
    run_id: str,
    workflow_id: str,
    modules: Mapping[str, ModuleSubmission],
    initial_scopes: Mapping[str, set[str]],
    lane_agent_invokers: Mapping[str, Mapping[str, AgentInvoker]],
    validate_subject: ModuleSubjectValidator,
    state_store: WorkflowStateStore,
    max_concurrency: int | None = None,
) -> dict[str, ModuleSubmission]:
    """Run and drain five branches, then publish only an all-complete Join."""

    definitions, contracts, workflow = build_module_cohort_definition(
        max_concurrency=max_concurrency,
    )
    module_ids = tuple(REPORT_TAXONOMY)
    workflow.state = {"module-inputs": dict(modules)}
    executors = build_builtin_executor_registry()
    plan = WorkflowCompiler(executors).compile(workflow, definitions)

    def lane_tool(module_id: str):
        async def execute(module_inputs: Any) -> DeclarativeModuleLaneOutcome:
            try:
                lane_result = await execute_declarative_module_lane(
                    run_id=run_id,
                    workflow_id=f"{workflow_id}--module-{module_id}",
                    module=ModuleSubmission.model_validate(
                        module_inputs[module_id]
                    ),
                    initial_scope=initial_scopes[module_id],
                    lifecycle_id="initial",
                    agent_invokers=lane_agent_invokers[module_id],
                    validate_subject=validate_subject,
                    state_store=InMemoryWorkflowStateStore(),
                    return_state=True,
                )
                result, lane_state = lane_result
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                return DeclarativeModuleLaneOutcome(
                    module_id=module_id,
                    status="failed",
                    error=str(exc),
                )
            return DeclarativeModuleLaneOutcome(
                module_id=module_id,
                status="completed",
                module=result,
                lane_state=lane_state.model_dump(mode="json"),
            )

        return execute

    tools = {
        f"execute-module-lane-{module_id}": lane_tool(module_id)
        for module_id in module_ids
    }
    tools["prepare-module-cohort"] = lambda value: value
    tools["reduce-module-cohort"] = _reduce_module_cohort
    try:
        state = state_store.load(run_id)
    except FileNotFoundError:
        state = WorkflowState.for_plan(run_id, plan)
    else:
        state = _retry_failed_module_lanes(plan, state, module_ids)
    completed = await WorkflowRuntimeHost(
        executors,
        state_store,
        InMemoryWorkflowEventSink(),
    ).execute(
        plan,
        state,
        RuntimeContext(tools=tools, contracts=contracts, definitions=definitions),
    )
    if completed.status is not WorkflowStatus.COMPLETED:
        raise DeclarativeModuleCohortError("declarative module cohort did not complete")

    return {
        module_id: ModuleSubmission.model_validate(value)
        for module_id, value in completed.outputs["result"].items()
    }


def _retry_failed_module_lanes(
    plan: ResolvedPlan,
    state: WorkflowState,
    module_ids: tuple[str, ...],
) -> WorkflowState:
    if state.status is not WorkflowStatus.FAILED:
        return state
    branches = state.parallel_results.get("module-cohort", {})
    failed = {
        module_id
        for module_id in module_ids
        if module_id in branches
        and DeclarativeModuleLaneOutcome.model_validate(
            branches[module_id][f"outcome-{module_id}"]
        ).status
        == "failed"
    }
    if not failed:
        return state
    return retry_parallel_branches(
        plan,
        state,
        parallel_action_id="module-cohort",
        branch_ids=failed,
    )


def _reduce_module_cohort(values: Mapping[str, Any]) -> dict[str, ModuleSubmission]:
    module_ids = tuple(REPORT_TAXONOMY)
    outcomes = {
        module_id: DeclarativeModuleLaneOutcome.model_validate(values[module_id])
        for module_id in module_ids
    }
    failures = {
        module_id: outcome.error or "module lane failed"
        for module_id, outcome in outcomes.items()
        if outcome.status == "failed"
    }
    if failures:
        raise DeclarativeModuleCohortError(
            "; ".join(
                f"{module_id}: {failures[module_id]}"
                for module_id in module_ids
                if module_id in failures
            )
        )
    return {
        module_id: ModuleSubmission.model_validate(outcomes[module_id].module)
        for module_id in module_ids
    }

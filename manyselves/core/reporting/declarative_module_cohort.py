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
from .declarative_module_runtime_lane import (
    DeclarativeModuleRuntimeLaneContext,
    register_module_runtime_lane_specializations,
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
    register_module_runtime_lane_specializations(registry)
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
    workflow.state["module-inputs"] = dict(modules)
    executors = build_builtin_executor_registry()
    plan = WorkflowCompiler(executors).compile(workflow, definitions)
    runtime_lane_plans = {
        workflow_id: WorkflowCompiler(executors).compile(
            definitions.require(DefinitionKind.WORKFLOW, workflow_id),
            definitions,
        )
        for workflow_id in (
            f"distribution-module-{module_id}-runtime-lane"
            for module_id in module_ids
        )
    }

    lane_runtime = _StandaloneModuleRuntime(
        run_id=run_id,
        workflow_id=workflow_id,
        initial_scopes=initial_scopes,
        lane_agent_invokers=lane_agent_invokers,
        validate_subject=validate_subject,
    )
    tools = {
        "start-current-module-lane": lambda values: lane_runtime.start_lane(
            str(values["module_id"]),
            values["state"],
            (
                DeclarativeModuleLaneOutcome.model_validate(
                    values["lane_outcomes"][str(values["module_id"])]
                )
                if str(values["module_id"]) in values.get("lane_outcomes", {})
                else None
            ),
        ),
        "prepare-current-module-authoring": lane_runtime.prepare_author_lane,
        "module-authoring-requires-agent": lane_runtime.author_requires_agent,
        "accept-current-module-authoring": lane_runtime.accept_author_lane,
        "resume-current-module-authoring": lane_runtime.resume_author_lane,
        "module-lane-can-review": lane_runtime.can_review_lane,
        "prepare-current-module-review": lane_runtime.prepare_review_lane,
        "module-review-requires-agent": lane_runtime.review_requires_agent,
        "accept-current-module-review": lane_runtime.accept_review_lane,
        "module-review-needs-revision": lane_runtime.review_needs_revision,
        "module-review-needs-recheck": lane_runtime.false_lane,
        "prepare-current-module-revision": lane_runtime.prepare_revision_lane,
        "accept-current-module-revision": lane_runtime.accept_revision_lane,
        "prepare-current-module-author-exception": (
            lane_runtime.prepare_author_exception_lane
        ),
        "prepare-current-module-recheck": lane_runtime.prepare_recheck_lane,
        "module-recheck-requires-agent": lane_runtime.recheck_requires_agent,
        "accept-current-module-recheck": lane_runtime.accept_recheck_lane,
        "resume-current-module-review": lane_runtime.resume_review_lane,
        "resume-current-module-recheck": lane_runtime.resume_recheck_lane,
        "continue-current-module-recheck": lane_runtime.continue_recheck_lane,
        "module-lane-has-deferred-main-exception": (
            lane_runtime.lane_has_deferred_main_exception
        ),
        "prepare-current-module-main-exception": lane_runtime.noop_lane,
        "module-main-exception-requires-agent": lane_runtime.false_lane,
        "accept-current-module-main-exception": lane_runtime.accept_passthrough,
        "module-main-exception-requests-user": lane_runtime.false_lane,
        "apply-current-module-main-exception-user-input": (
            lane_runtime.accept_passthrough
        ),
        "route-current-module-after-main-exception": lane_runtime.noop_lane,
        "complete-current-module-lane": lane_runtime.complete_lane,
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
        RuntimeContext(
            tools=tools,
            contracts=contracts,
            definitions=definitions,
            subworkflows=runtime_lane_plans,
        ),
    )
    if completed.status is not WorkflowStatus.COMPLETED:
        raise DeclarativeModuleCohortError("declarative module cohort did not complete")

    return {
        module_id: ModuleSubmission.model_validate(value)
        for module_id, value in completed.outputs["result"].items()
    }


class _StandaloneModuleRuntime:
    """Bind the reusable characterization Lane to the production Lane graph."""

    def __init__(
        self,
        *,
        run_id: str,
        workflow_id: str,
        initial_scopes: Mapping[str, set[str]],
        lane_agent_invokers: Mapping[str, Mapping[str, AgentInvoker]],
        validate_subject: ModuleSubjectValidator,
    ) -> None:
        self._run_id = run_id
        self._workflow_id = workflow_id
        self._initial_scopes = initial_scopes
        self._lane_agent_invokers = lane_agent_invokers
        self._validate_subject = validate_subject

    async def start_lane(
        self,
        module_id: str,
        module_inputs: Mapping[str, Any],
        lane_outcome: DeclarativeModuleLaneOutcome | None = None,
    ) -> DeclarativeModuleRuntimeLaneContext:
        if lane_outcome is not None:
            return DeclarativeModuleRuntimeLaneContext(
                module_id=module_id,
                workflow_id=f"{self._workflow_id}--module-{module_id}",
                reporting_state=dict(module_inputs),
                status=(
                    "completed" if lane_outcome.status == "completed" else "failed"
                ),
                module=lane_outcome.module,
                error=lane_outcome.error,
            )
        return DeclarativeModuleRuntimeLaneContext(
            module_id=module_id,
            workflow_id=f"{self._workflow_id}--module-{module_id}",
            reporting_state=dict(module_inputs),
            status="ready",
            module=ModuleSubmission.model_validate(module_inputs[module_id]),
        )

    async def prepare_author_lane(
        self,
        context: DeclarativeModuleRuntimeLaneContext,
    ) -> DeclarativeModuleRuntimeLaneContext:
        if context.status != "ready":
            return context
        return context.model_copy(update={"status": "author_resumed"})

    async def author_requires_agent(
        self,
        _context: DeclarativeModuleRuntimeLaneContext,
    ) -> bool:
        return False

    async def accept_author_lane(
        self,
        values: Mapping[str, Any],
    ) -> DeclarativeModuleRuntimeLaneContext:
        return DeclarativeModuleRuntimeLaneContext.model_validate(values["context"])

    async def resume_author_lane(
        self,
        context: DeclarativeModuleRuntimeLaneContext,
    ) -> DeclarativeModuleRuntimeLaneContext:
        if context.status != "author_resumed":
            return context
        return context.model_copy(update={"status": "authored"})

    async def can_review_lane(
        self,
        context: DeclarativeModuleRuntimeLaneContext,
    ) -> bool:
        return context.status == "authored"

    async def prepare_review_lane(
        self,
        context: DeclarativeModuleRuntimeLaneContext,
    ) -> DeclarativeModuleRuntimeLaneContext:
        if context.status != "authored":
            return context
        return context.model_copy(update={"status": "review_resumed"})

    async def review_requires_agent(
        self,
        _context: DeclarativeModuleRuntimeLaneContext,
    ) -> bool:
        return False

    async def accept_review_lane(
        self,
        values: Mapping[str, Any],
    ) -> DeclarativeModuleRuntimeLaneContext:
        return DeclarativeModuleRuntimeLaneContext.model_validate(values["context"])

    async def review_needs_revision(
        self,
        _context: DeclarativeModuleRuntimeLaneContext,
    ) -> bool:
        return False

    async def prepare_revision_lane(
        self,
        context: DeclarativeModuleRuntimeLaneContext,
    ) -> DeclarativeModuleRuntimeLaneContext:
        return context

    async def accept_revision_lane(
        self,
        values: Mapping[str, Any],
    ) -> DeclarativeModuleRuntimeLaneContext:
        return DeclarativeModuleRuntimeLaneContext.model_validate(values["context"])

    async def prepare_author_exception_lane(
        self,
        context: DeclarativeModuleRuntimeLaneContext,
    ) -> DeclarativeModuleRuntimeLaneContext:
        return context

    async def prepare_recheck_lane(
        self,
        context: DeclarativeModuleRuntimeLaneContext,
    ) -> DeclarativeModuleRuntimeLaneContext:
        return context

    async def recheck_requires_agent(
        self,
        _context: DeclarativeModuleRuntimeLaneContext,
    ) -> bool:
        return False

    async def accept_recheck_lane(
        self,
        values: Mapping[str, Any],
    ) -> DeclarativeModuleRuntimeLaneContext:
        return DeclarativeModuleRuntimeLaneContext.model_validate(values["context"])

    async def resume_review_lane(
        self,
        context: DeclarativeModuleRuntimeLaneContext,
    ) -> DeclarativeModuleRuntimeLaneContext:
        try:
            lane_result = await execute_declarative_module_lane(
                run_id=self._run_id,
                workflow_id=context.workflow_id,
                module=ModuleSubmission.model_validate(context.module),
                initial_scope=self._initial_scopes[context.module_id],
                lifecycle_id="initial",
                agent_invokers=self._lane_agent_invokers[context.module_id],
                validate_subject=self._validate_subject,
                state_store=InMemoryWorkflowStateStore(),
                return_state=True,
            )
            result, lane_state = lane_result
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return context.model_copy(
                update={"status": "failed", "error": str(exc)}
            )
        return context.model_copy(
            update={
                "status": "reviewed",
                "module": result,
                "reporting_state": lane_state.model_dump(mode="json"),
            }
        )

    async def resume_recheck_lane(
        self,
        context: DeclarativeModuleRuntimeLaneContext,
    ) -> DeclarativeModuleRuntimeLaneContext:
        return context

    async def continue_recheck_lane(
        self,
        context: DeclarativeModuleRuntimeLaneContext,
    ) -> DeclarativeModuleRuntimeLaneContext:
        return context

    async def lane_has_deferred_main_exception(
        self,
        _context: DeclarativeModuleRuntimeLaneContext,
    ) -> bool:
        return False

    async def noop_lane(
        self,
        context: DeclarativeModuleRuntimeLaneContext,
    ) -> DeclarativeModuleRuntimeLaneContext:
        return context

    async def false_lane(
        self,
        _context: DeclarativeModuleRuntimeLaneContext,
    ) -> bool:
        return False

    async def accept_passthrough(
        self,
        values: Mapping[str, Any],
    ) -> DeclarativeModuleRuntimeLaneContext:
        return DeclarativeModuleRuntimeLaneContext.model_validate(values["context"])

    async def complete_lane(
        self,
        context: DeclarativeModuleRuntimeLaneContext,
    ) -> DeclarativeModuleLaneOutcome:
        status = (
            "completed" if context.status in {"reviewed", "completed"} else "failed"
        )
        return DeclarativeModuleLaneOutcome(
            module_id=context.module_id,
            status=status,
            module=context.module if status == "completed" else None,
            error=context.error,
            lane_state=context.reporting_state if status == "completed" else None,
        )


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

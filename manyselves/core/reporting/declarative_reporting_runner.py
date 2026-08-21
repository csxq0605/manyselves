"""Explicit declarative path over the complete current Reporting lifecycle."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol, cast

from manyselves.capabilities.distribution_reporting import (
    load_distribution_reporting_capability,
)
from manyselves.capabilities.distribution_reporting.adapters import (
    project_reporting_agent,
)
from manyselves.kernel.contracts import ContractAdapter, build_contract_catalog
from manyselves.kernel.conversations import ConversationRecord, ConversationRegistry
from manyselves.kernel.definitions import (
    AgentDefinition,
    DefinitionKind,
    DefinitionRegistry,
    RecoveryPolicyDefinition,
    TaskDefinition,
    WorkflowDefinition,
    specialize_workflow,
)
from manyselves.kernel.executors import (
    ActionResult,
    ExecutorRegistry,
    RuntimeContext,
    build_builtin_executor_registry,
)
from manyselves.kernel.ports import (
    AgentInvocationOutcome,
    AgentInvoker,
    WorkflowStateStore,
)
from manyselves.kernel.workflow import (
    ResolvedPlan,
    WorkflowCompiler,
    WorkflowState,
    WorkflowStatus,
    apply_action_result,
    restore_plan_definition_registry,
    resume_waiting_input,
)
from manyselves.runtime.conversation_store import FileConversationStore
from manyselves.runtime.state_store import FileWorkflowStateStore
from manyselves.runtime.tool_adapter import CapabilityToolAdapterFactory
from manyselves.runtime.workflow_host import (
    FileWorkflowEventSink,
    InMemoryWorkflowEventSink,
    WorkflowEventSink,
    WorkflowRuntimeHost,
)

from .agentic_models import TaskEnvelope, WorkflowDecisionSubmission
from .declarative_chief_chapter_cohort import (
    DeclarativeChiefChapterRuntime,
    register_chief_chapter_lane_specializations,
    retry_failed_chief_chapter_lanes,
)
from .declarative_cross_owner_cohort import (
    DeclarativeCrossOwnerRuntime,
    DeclarativeMainExceptionAgentResult,
    DeclarativeMainExceptionUserInput,
    register_cross_owner_pipeline_specializations,
    retry_failed_cross_owner_pipelines,
)
from .declarative_delivery import (
    DeclarativeDeliveryRuntime,
)
from .declarative_final_chapter_cohort import (
    DeclarativeFinalChapterRuntime,
    register_final_chapter_lane_specializations,
    retry_failed_final_chapter_lanes,
)
from .declarative_final_review_cycle import (
    DeclarativeFinalReviewRuntime,
    compose_final_review_agent_invokers,
    register_final_review_lane_specializations,
)
from .declarative_module_cohort import (
    DeclarativeModuleLaneOutcome,
    _retry_failed_module_lanes,
)
from .declarative_module_runtime_lane import (
    DeclarativeModuleAuthoringAgentResult,
    DeclarativeModuleAuthoringPreparation,
    DeclarativeModuleLaneAttempt,
    DeclarativeModuleRecheckAgentResult,
    DeclarativeModuleRecheckPreparation,
    DeclarativeModuleReviewAgentResult,
    DeclarativeModuleReviewPreparation,
    DeclarativeModuleRevisionAgentResult,
    DeclarativeModuleRevisionPreparation,
    DeclarativeModuleRuntimeLaneContext,
    register_module_runtime_lane_specializations,
)
from .declarative_reporting_tail import _ReportingTailAdapters
from .declarative_task_binding import bind_declared_task
from .distributed_runtime import LocalEventStore
from .models import REPORT_MODULE_IDS
from .parallel_runtime import LaneCompletion
from .review_lifecycle import (
    DeferredMainDecision,
    MainExceptionDecisionPreparation,
    ReviewLifecycleError,
    accept_main_exception_decision,
    prepare_main_exception_decision,
)
from .taxonomy import REPORT_TAXONOMY
from .workflow import (
    AgentWorkflowError,
    ReportingNeedsDecisionError,
    ReportWorkflowRunner,
    _ModuleAuthoringPreparationContext,
    _ModuleLaneAttemptContext,
)


class ReportingModuleRuntime(Protocol):
    """Capability adapter bound to the file-defined module Cohort actions."""

    current_state: dict[str, Any] | None
    agent_invokers: Mapping[str, AgentInvoker]

    async def prepare_lanes(self, state: dict[str, Any]) -> dict[str, Any]: ...

    async def start_lane(
        self,
        module_id: str,
        state: dict[str, Any],
        workflow_id: str,
        lane_outcome: DeclarativeModuleLaneOutcome | None = None,
    ) -> DeclarativeModuleRuntimeLaneContext: ...

    async def prepare_author_lane(
        self,
        context: DeclarativeModuleRuntimeLaneContext,
    ) -> DeclarativeModuleRuntimeLaneContext: ...

    async def author_requires_agent(
        self,
        context: DeclarativeModuleRuntimeLaneContext,
    ) -> bool: ...

    async def accept_author_lane(
        self,
        values: Mapping[str, Any],
    ) -> DeclarativeModuleRuntimeLaneContext: ...

    async def resume_author_lane(
        self,
        context: DeclarativeModuleRuntimeLaneContext,
    ) -> DeclarativeModuleRuntimeLaneContext: ...

    async def can_review_lane(
        self,
        context: DeclarativeModuleRuntimeLaneContext,
    ) -> bool: ...

    async def prepare_review_lane(
        self,
        context: DeclarativeModuleRuntimeLaneContext,
    ) -> DeclarativeModuleRuntimeLaneContext: ...

    async def review_preflight_needs_revision(
        self,
        context: DeclarativeModuleRuntimeLaneContext,
    ) -> bool: ...

    async def prepare_preflight_revision_lane(
        self,
        context: DeclarativeModuleRuntimeLaneContext,
    ) -> DeclarativeModuleRuntimeLaneContext: ...

    async def accept_preflight_revision_lane(
        self,
        values: Mapping[str, Any],
    ) -> DeclarativeModuleRuntimeLaneContext: ...

    async def review_requires_agent(
        self,
        context: DeclarativeModuleRuntimeLaneContext,
    ) -> bool: ...

    async def accept_review_lane(
        self,
        values: Mapping[str, Any],
    ) -> DeclarativeModuleRuntimeLaneContext: ...

    async def review_needs_revision(
        self,
        context: DeclarativeModuleRuntimeLaneContext,
    ) -> bool: ...

    async def review_needs_recheck(
        self,
        context: DeclarativeModuleRuntimeLaneContext,
    ) -> bool: ...

    async def prepare_revision_lane(
        self,
        context: DeclarativeModuleRuntimeLaneContext,
    ) -> DeclarativeModuleRuntimeLaneContext: ...

    async def accept_revision_lane(
        self,
        values: Mapping[str, Any],
    ) -> DeclarativeModuleRuntimeLaneContext: ...

    async def prepare_author_exception_lane(
        self,
        context: DeclarativeModuleRuntimeLaneContext,
    ) -> DeclarativeModuleRuntimeLaneContext: ...

    async def prepare_recheck_lane(
        self,
        context: DeclarativeModuleRuntimeLaneContext,
    ) -> DeclarativeModuleRuntimeLaneContext: ...

    async def recheck_requires_agent(
        self,
        context: DeclarativeModuleRuntimeLaneContext,
    ) -> bool: ...

    async def accept_recheck_lane(
        self,
        values: Mapping[str, Any],
    ) -> DeclarativeModuleRuntimeLaneContext: ...

    async def resume_review_lane(
        self,
        context: DeclarativeModuleRuntimeLaneContext,
    ) -> DeclarativeModuleRuntimeLaneContext: ...

    async def resume_recheck_lane(
        self,
        context: DeclarativeModuleRuntimeLaneContext,
    ) -> DeclarativeModuleRuntimeLaneContext: ...

    async def lane_has_deferred_main_exception(
        self,
        context: DeclarativeModuleRuntimeLaneContext,
    ) -> bool: ...

    async def lane_retries_preflight_revision(
        self,
        context: DeclarativeModuleRuntimeLaneContext,
    ) -> bool: ...

    async def preflight_revision_needs_recheck(
        self,
        context: DeclarativeModuleRuntimeLaneContext,
    ) -> bool: ...

    async def prepare_main_exception_lane(
        self,
        context: DeclarativeModuleRuntimeLaneContext,
    ) -> DeclarativeModuleRuntimeLaneContext: ...

    async def main_exception_requires_agent(
        self,
        context: DeclarativeModuleRuntimeLaneContext,
    ) -> bool: ...

    async def accept_main_exception_lane(
        self,
        values: Mapping[str, Any],
    ) -> DeclarativeModuleRuntimeLaneContext: ...

    async def main_exception_requests_user(
        self,
        context: DeclarativeModuleRuntimeLaneContext,
    ) -> bool: ...

    async def apply_main_exception_user_input(
        self,
        values: Mapping[str, Any],
    ) -> DeclarativeModuleRuntimeLaneContext: ...

    async def route_after_main_exception(
        self,
        context: DeclarativeModuleRuntimeLaneContext,
    ) -> DeclarativeModuleRuntimeLaneContext: ...

    async def complete_lane(
        self,
        context: DeclarativeModuleRuntimeLaneContext,
    ) -> DeclarativeModuleLaneOutcome: ...

    async def reduce_lanes(self, outcomes: Mapping[str, Any]) -> dict[str, Any]: ...


class _TaskScopedAgentInvoker:
    """Route one reused Agent identity by its exact declared Task."""

    def __init__(
        self,
        default: AgentInvoker,
        task_routes: Mapping[str, AgentInvoker],
    ) -> None:
        self._default = default
        self._task_routes = task_routes

    async def invoke(
        self,
        agent: AgentDefinition,
        task: TaskDefinition,
        value: Any,
        conversation: ConversationRecord,
        *,
        task_id: str,
    ) -> AgentInvocationOutcome:
        return await self._invoke(
            agent,
            task,
            value,
            conversation,
            task_id=task_id,
            recovery_policy=None,
        )

    async def invoke_with_recovery(
        self,
        agent: AgentDefinition,
        task: TaskDefinition,
        value: Any,
        conversation: ConversationRecord,
        *,
        task_id: str,
        recovery_policy: RecoveryPolicyDefinition,
    ) -> AgentInvocationOutcome:
        return await self._invoke(
            agent,
            task,
            value,
            conversation,
            task_id=task_id,
            recovery_policy=recovery_policy,
        )

    async def _invoke(
        self,
        agent: AgentDefinition,
        task: TaskDefinition,
        value: Any,
        conversation: ConversationRecord,
        *,
        task_id: str,
        recovery_policy: RecoveryPolicyDefinition | None,
    ) -> AgentInvocationOutcome:
        invoker = self._task_routes.get(task.id, self._default)
        if recovery_policy is None:
            return await invoker.invoke(
                agent,
                task,
                value,
                conversation,
                task_id=task_id,
            )
        invoke_with_recovery = getattr(invoker, "invoke_with_recovery", None)
        if not callable(invoke_with_recovery):
            raise RuntimeError(
                f"routed agent adapter does not support declared recovery: {agent.id}"
            )
        return await invoke_with_recovery(
            agent,
            task,
            value,
            conversation,
            task_id=task_id,
            recovery_policy=recovery_policy,
        )


def _reporting_agent_invokers(
    module_invokers: Mapping[str, AgentInvoker],
    cross_invokers: Mapping[str, AgentInvoker],
    chief_invokers: Mapping[str, AgentInvoker] | None = None,
    final_invokers: Mapping[str, AgentInvoker] | None = None,
) -> dict[str, AgentInvoker]:
    """Compose module, Cross, and Chief adapters for the declared runtime."""

    combined = dict(module_invokers)
    for agent_id, cross_invoker in cross_invokers.items():
        module_invoker = combined.get(agent_id)
        if module_invoker is None:
            combined[agent_id] = cross_invoker
            continue
        if agent_id == "evidence-auditor":
            task_routes = {
                "cross-owner-runtime-local-review": cross_invoker,
            }
        else:
            module_id = agent_id.removeprefix("module-").removesuffix("-specialist")
            task_routes = {
                f"cross-owner-module-{module_id}-revision-r1": cross_invoker,
            }
        combined[agent_id] = _TaskScopedAgentInvoker(
            module_invoker,
            task_routes,
        )
    combined.update(chief_invokers or {})
    combined.update(final_invokers or {})
    return combined


def build_reporting_module_stage_definition() -> tuple[DefinitionRegistry, WorkflowDefinition]:
    """Compatibility loader for the packaged top-level Reporting workflow."""

    _capability, registry = load_distribution_reporting_capability()
    register_cross_owner_pipeline_specializations(registry)
    register_chief_chapter_lane_specializations(registry)
    register_final_chapter_lane_specializations(registry)
    register_final_review_lane_specializations(registry)
    workflow = registry.require(
        DefinitionKind.WORKFLOW,
        "distribution-reporting",
    )
    if not isinstance(workflow, WorkflowDefinition):
        raise TypeError("distribution-reporting is not a workflow")
    return registry, workflow.model_copy(deep=True)


@dataclass(frozen=True)
class _CompiledReportingRuntime:
    definitions: DefinitionRegistry
    contracts: dict[str, ContractAdapter]
    executors: ExecutorRegistry
    plan: ResolvedPlan
    cohort_plan: ResolvedPlan
    cross_cohort_plan: ResolvedPlan
    chief_cohort_plan: ResolvedPlan
    final_cohort_plan: ResolvedPlan
    subworkflows: dict[str, ResolvedPlan]


def _compile_reporting_runtime(
    reporting_state: Mapping[str, Any],
    *,
    full_report: bool,
) -> _CompiledReportingRuntime:
    """Compile the top-level plan and every file-defined nested workflow."""

    definitions, workflow = build_reporting_module_stage_definition()
    register_module_runtime_lane_specializations(definitions)
    cohort_template = definitions.require(
        DefinitionKind.WORKFLOW,
        "distribution-module-cohort",
    )
    if not isinstance(cohort_template, WorkflowDefinition):
        raise TypeError("distribution-module-cohort is not a workflow")
    cohort = specialize_workflow(
        cohort_template,
        {"max_concurrency": len(REPORT_MODULE_IDS)},
    )
    specialized_definitions = DefinitionRegistry()
    for definition in definitions.all():
        specialized_definitions.register(
            cohort
            if definition.kind is DefinitionKind.WORKFLOW
            and definition.id == cohort.id
            else definition
        )
    definitions = specialized_definitions
    workflow.state = {
        "reporting-state": deepcopy(dict(reporting_state)),
        "full-report": full_report,
    }
    executors = build_builtin_executor_registry()
    compiler = WorkflowCompiler(executors)
    plan = compiler.compile(workflow, definitions)
    subworkflows = plan.subworkflow_plans
    cohort_plan = subworkflows[cohort.id]
    cross_cohort_plan = subworkflows["distribution-cross-owner-cohort"]
    chief_cohort_plan = subworkflows["distribution-chief-chapter-cohort"]
    final_cohort_plan = subworkflows["distribution-final-chapter-cohort"]
    return _CompiledReportingRuntime(
        definitions=definitions,
        contracts=build_contract_catalog(definitions),
        executors=executors,
        plan=plan,
        cohort_plan=cohort_plan,
        cross_cohort_plan=cross_cohort_plan,
        chief_cohort_plan=chief_cohort_plan,
        final_cohort_plan=final_cohort_plan,
        subworkflows=subworkflows,
    )


def _assemble_reporting_capability_tools(
    definitions: DefinitionRegistry,
    contracts: Mapping[str, ContractAdapter],
    implementations: Mapping[str, Callable[[Any], Any]],
    *plans: ResolvedPlan,
) -> dict[str, Any]:
    """Bind every invoked Reporting Capability Tool through its declaration."""

    bound = dict(implementations)
    factory = CapabilityToolAdapterFactory(
        "distribution-reporting",
        implementations,
        contracts,
    )
    seen: set[str] = set()
    for plan in plans:
        for tool_id in plan.tool_ids:
            if tool_id in seen:
                continue
            seen.add(tool_id)
            definition = definitions.require(DefinitionKind.TOOL, tool_id)
            implementation = plan.tool_implementations.get(tool_id)
            if implementation is not None:
                definition = definition.model_copy(
                    update={"implementation": implementation}
                )
            bound[tool_id] = factory.build(definition)
    return bound


def resume_declarative_reporting_input(
    *,
    workspace: Path,
    run_id: str,
    input_id: str,
    values: object,
) -> WorkflowState:
    """Validate and apply one generic Interaction input to its persisted Run."""

    state_store = FileWorkflowStateStore(workspace)
    state = state_store.load(run_id)
    saved_plan = state_store.load_plan(run_id)
    if saved_plan.definition_snapshots:
        definitions = restore_plan_definition_registry(saved_plan)
        subworkflows = saved_plan.subworkflow_plans
    else:
        reporting_state = state.variables.get("reporting-state", {})
        compiled = _compile_reporting_runtime(
            reporting_state if isinstance(reporting_state, Mapping) else {},
            full_report=bool(state.variables.get("full-report", False)),
        )
        definitions = compiled.definitions
        subworkflows = saved_plan.subworkflow_plans or compiled.subworkflows
    resumed = resume_waiting_input(
        saved_plan,
        state,
        input_id=input_id,
        values=values,
        contracts=build_contract_catalog(definitions),
        subworkflows=subworkflows,
    )
    state_store.save(resumed)
    return resumed


async def execute_declarative_module_stage(
    *,
    requested_modules: tuple[str, ...],
    state: dict[str, Any],
    workflow_id: str,
    state_store: WorkflowStateStore,
    tail_runner: Any | None = None,
    event_sink: WorkflowEventSink | None = None,
    module_runtime: ReportingModuleRuntime | None = None,
    execute_current: (
        Callable[[tuple[str, ...], dict[str, Any], str], Awaitable[None]] | None
    ) = None,
    workspace: Path | None = None,
    stage_boundary: (
        Callable[[dict[str, Any], str, str], Awaitable[None]] | None
    ) = None,
) -> WorkflowState:
    """Run current modules and the file-defined tail in one parent state."""

    full_report = set(requested_modules) == set(REPORT_MODULE_IDS)
    kernel_run_id = str(state["run_id"])
    saved_plan: ResolvedPlan | None = None
    load_plan = getattr(state_store, "load_plan", None)
    if callable(load_plan):
        try:
            state_store.load(kernel_run_id)
            saved_plan = load_plan(kernel_run_id)
        except FileNotFoundError:
            pass
    if (
        saved_plan is not None
        and saved_plan.definition_snapshots
        and saved_plan.subworkflow_plans
    ):
        compiled = None
        plan = saved_plan
        definitions = restore_plan_definition_registry(plan)
        executors = build_builtin_executor_registry()
        subworkflows = plan.subworkflow_plans
        cohort_plan = subworkflows["distribution-module-cohort"]
        cross_cohort_plan = subworkflows["distribution-cross-owner-cohort"]
        chief_cohort_plan = subworkflows["distribution-chief-chapter-cohort"]
        final_cohort_plan = subworkflows["distribution-final-chapter-cohort"]
    else:
        compiled = _compile_reporting_runtime(state, full_report=full_report)
        definitions = compiled.definitions
        executors = compiled.executors
        plan = compiled.plan
        cohort_plan = compiled.cohort_plan
        cross_cohort_plan = compiled.cross_cohort_plan
        chief_cohort_plan = compiled.chief_cohort_plan
        final_cohort_plan = compiled.final_cohort_plan
    if module_runtime is None:
        if execute_current is None:
            raise TypeError("module runtime is required")
        module_runtime = _BatchModuleRuntime(
            execute_current,
            requested_modules,
            state,
            workflow_id,
        )
    try:
        kernel_state = state_store.load(kernel_run_id)
        load_plan = getattr(state_store, "load_plan", None)
        if callable(load_plan):
            try:
                plan = load_plan(kernel_run_id)
            except FileNotFoundError:
                save_plan = getattr(state_store, "save_plan", None)
                if callable(save_plan):
                    save_plan(kernel_run_id, plan)
        subworkflows = plan.subworkflow_plans or (
            compiled.subworkflows if compiled is not None else {}
        )
        cohort_plan = subworkflows.get(
            "distribution-module-cohort",
            cohort_plan,
        )
        cross_cohort_plan = subworkflows.get(
            "distribution-cross-owner-cohort",
            cross_cohort_plan,
        )
        chief_cohort_plan = subworkflows.get(
            "distribution-chief-chapter-cohort",
            chief_cohort_plan,
        )
        final_cohort_plan = subworkflows.get(
            "distribution-final-chapter-cohort",
            final_cohort_plan,
        )
        kernel_state = apply_action_result(
            kernel_state,
            ActionResult(
                variable_updates={
                    "reporting-state": deepcopy(state),
                    "full-report": full_report,
                }
            ),
        )
        saved_tail = kernel_state.subworkflow_states.get("run-reporting-tail")
        if saved_tail is not None:
            child = apply_action_result(
                WorkflowState.model_validate(saved_tail),
                ActionResult(
                    variable_updates={"reporting-state": deepcopy(state)}
                ),
            )
            saved_cross = child.subworkflow_states.get("run-cross")
            if saved_cross is not None:
                cross_state = apply_action_result(
                    WorkflowState.model_validate(saved_cross),
                    ActionResult(
                        variable_updates={
                            "reporting-state": deepcopy(state),
                            "prepared-cross-state": deepcopy(state),
                        }
                    ),
                )
                cross_state = retry_failed_cross_owner_pipelines(
                    cross_cohort_plan,
                    cross_state,
                )
                child = apply_action_result(
                    child,
                    ActionResult(
                        subworkflow_state_updates={
                            "run-cross": cross_state.model_dump(mode="json")
                        }
                    ),
                )
            saved_chief = child.subworkflow_states.get("run-chief")
            if saved_chief is not None:
                chief_state = apply_action_result(
                    WorkflowState.model_validate(saved_chief),
                    ActionResult(
                        variable_updates={
                            "reporting-state": deepcopy(state),
                            "prepared-chief-state": deepcopy(state),
                        }
                    ),
                )
                chief_state = retry_failed_chief_chapter_lanes(
                    chief_cohort_plan,
                    chief_state,
                )
                child = apply_action_result(
                    child,
                    ActionResult(
                        subworkflow_state_updates={
                            "run-chief": chief_state.model_dump(mode="json")
                        }
                    ),
                )
            saved_final = child.subworkflow_states.get("run-final")
            if saved_final is not None:
                final_state = apply_action_result(
                    WorkflowState.model_validate(saved_final),
                    ActionResult(
                        variable_updates={
                            "reporting-state": deepcopy(state),
                            "prepared-final-state": deepcopy(state),
                        }
                    ),
                )
                final_state = retry_failed_final_chapter_lanes(
                    final_cohort_plan,
                    final_state,
                    subworkflows=subworkflows,
                )
                child = apply_action_result(
                    child,
                    ActionResult(
                        subworkflow_state_updates={
                            "run-final": final_state.model_dump(mode="json")
                        }
                    ),
                )
            kernel_state = apply_action_result(
                kernel_state,
                ActionResult(
                    subworkflow_state_updates={
                        "run-reporting-tail": child.model_dump(mode="json")
                    }
                ),
            )
        saved_cohort = kernel_state.subworkflow_states.get("run-module-cohort")
        if saved_cohort is not None:
            child = apply_action_result(
                WorkflowState.model_validate(saved_cohort),
                ActionResult(
                    variable_updates={
                        "module-inputs": deepcopy(state),
                        "prepared-module-inputs": deepcopy(state),
                    }
                ),
            )
            child = _retry_failed_module_lanes(
                cohort_plan,
                child,
                tuple(REPORT_MODULE_IDS),
            )
            kernel_state = apply_action_result(
                kernel_state,
                ActionResult(
                    subworkflow_state_updates={
                        "run-module-cohort": child.model_dump(mode="json")
                    }
                ),
            )
    except FileNotFoundError:
        kernel_state = WorkflowState.for_plan(kernel_run_id, plan)
        save_plan = getattr(state_store, "save_plan", None)
        if callable(save_plan):
            save_plan(kernel_run_id, plan)
        subworkflows = plan.subworkflow_plans or (
            compiled.subworkflows if compiled is not None else {}
        )

    definitions = (
        restore_plan_definition_registry(plan)
        if plan.definition_snapshots
        else cast(_CompiledReportingRuntime, compiled).definitions
    )
    contracts = build_contract_catalog(definitions)

    tail_adapters = _ReportingTailAdapters(tail_runner, workflow_id)
    cross_runtime = DeclarativeCrossOwnerRuntime(
        tail_runner,
        state,
        workflow_id,
        compatibility_cross=tail_adapters.cross,
    )
    chief_runtime = DeclarativeChiefChapterRuntime(
        tail_runner,
        state,
        workflow_id,
        compatibility_chief=tail_adapters.chief,
    )
    final_runtime = DeclarativeFinalChapterRuntime(
        tail_runner,
        state,
        workflow_id,
    )
    final_review_runtime = DeclarativeFinalReviewRuntime(
        tail_runner,
        state,
        workflow_id,
    )
    delivery_runtime = DeclarativeDeliveryRuntime(tail_runner)

    async def prepare_cross_with_boundary(
        current_state: dict[str, Any],
    ) -> dict[str, Any]:
        if stage_boundary is not None:
            await stage_boundary(
                current_state,
                "module-work",
                "cross-module-review",
            )
        return cross_runtime.prepare(current_state)

    async def prepare_chief_with_boundary(
        current_state: dict[str, Any],
    ) -> dict[str, Any]:
        if stage_boundary is not None:
            await stage_boundary(
                current_state,
                "cross-module-review",
                "chief-edit",
            )
        return chief_runtime.prepare(current_state)

    async def prepare_final_with_boundary(
        current_state: dict[str, Any],
    ) -> dict[str, Any]:
        if stage_boundary is not None:
            await stage_boundary(
                current_state,
                "chief-edit",
                "chief-editor-audit",
            )
        return final_runtime.prepare(current_state)

    async def prepare_delivery_with_boundary(
        current_state: dict[str, Any],
    ) -> dict[str, Any]:
        if stage_boundary is not None:
            await stage_boundary(
                current_state,
                "chief-editor-audit",
                "delivery",
            )
        return delivery_runtime.prepare(current_state)
    module_tools = {
        "start-current-module-lane": lambda values: module_runtime.start_lane(
            str(values["module_id"]),
            values["state"],
            workflow_id,
            (
                DeclarativeModuleLaneOutcome.model_validate(
                    values["lane_outcomes"][str(values["module_id"])]
                )
                if str(values["module_id"]) in values.get("lane_outcomes", {})
                else None
            ),
        ),
        "prepare-current-module-authoring": module_runtime.prepare_author_lane,
        "module-authoring-requires-agent": module_runtime.author_requires_agent,
        "accept-current-module-authoring": module_runtime.accept_author_lane,
        "resume-current-module-authoring": module_runtime.resume_author_lane,
        "module-lane-can-review": module_runtime.can_review_lane,
        "prepare-current-module-review": module_runtime.prepare_review_lane,
        "module-review-preflight-needs-revision": (module_runtime.review_preflight_needs_revision),
        "prepare-current-module-preflight-revision": (
            module_runtime.prepare_preflight_revision_lane
        ),
        "accept-current-module-preflight-revision": (module_runtime.accept_preflight_revision_lane),
        "module-review-requires-agent": module_runtime.review_requires_agent,
        "accept-current-module-review": module_runtime.accept_review_lane,
        "module-review-needs-revision": module_runtime.review_needs_revision,
        "module-review-needs-recheck": module_runtime.review_needs_recheck,
        "prepare-current-module-revision": module_runtime.prepare_revision_lane,
        "accept-current-module-revision": module_runtime.accept_revision_lane,
        "prepare-current-module-author-exception": (module_runtime.prepare_author_exception_lane),
        "prepare-current-module-recheck": module_runtime.prepare_recheck_lane,
        "module-recheck-requires-agent": module_runtime.recheck_requires_agent,
        "accept-current-module-recheck": module_runtime.accept_recheck_lane,
        "resume-current-module-review": module_runtime.resume_review_lane,
        "resume-current-module-recheck": module_runtime.resume_recheck_lane,
        "module-lane-has-deferred-main-exception": (
            module_runtime.lane_has_deferred_main_exception
        ),
        "module-lane-retries-preflight-revision": (module_runtime.lane_retries_preflight_revision),
        "module-preflight-revision-needs-recheck": (
            module_runtime.preflight_revision_needs_recheck
        ),
        "prepare-current-module-main-exception": (module_runtime.prepare_main_exception_lane),
        "module-main-exception-requires-agent": (module_runtime.main_exception_requires_agent),
        "accept-current-module-main-exception": (module_runtime.accept_main_exception_lane),
        "module-main-exception-requests-user": (module_runtime.main_exception_requests_user),
        "apply-current-module-main-exception-user-input": (
            module_runtime.apply_main_exception_user_input
        ),
        "route-current-module-after-main-exception": (module_runtime.route_after_main_exception),
        "complete-current-module-lane": module_runtime.complete_lane,
    }
    module_tools["prepare-module-cohort"] = module_runtime.prepare_lanes
    module_tools["reduce-module-cohort"] = module_runtime.reduce_lanes
    runtime_tools = {
        **module_tools,
        "prepare-cross-owner-cohort": prepare_cross_with_boundary,
        "prepare-current-cross-owner-initial": cross_runtime.prepare_initial,
        "cross-owner-initial-requires-agent": (cross_runtime.initial_requires_agent),
        "accept-current-cross-owner-initial": cross_runtime.accept_initial,
        "cross-owner-initial-has-findings": (cross_runtime.initial_has_findings),
        "prepare-current-cross-owner-revision": (cross_runtime.prepare_revision),
        "cross-owner-revision-requires-agent": (cross_runtime.revision_requires_agent),
        "accept-current-cross-owner-revision": (cross_runtime.accept_revision),
        "prepare-current-cross-owner-author-exception": (cross_runtime.prepare_author_exception),
        "prepare-current-cross-owner-reviewer-exception": (
            cross_runtime.prepare_reviewer_exception
        ),
        "cross-owner-main-exception-requires-agent": (cross_runtime.main_exception_requires_agent),
        "accept-current-cross-owner-main-exception": (cross_runtime.accept_main_exception),
        "cross-owner-main-exception-requests-user": (cross_runtime.main_exception_requests_user),
        "apply-current-cross-owner-main-exception-user-input": (
            cross_runtime.apply_main_exception_user_input
        ),
        "cross-owner-author-exception-returns-to-author": (
            cross_runtime.author_exception_returns_to_author
        ),
        "prepare-current-cross-owner-local-review": (cross_runtime.prepare_local_review),
        "cross-owner-local-review-requires-agent": (cross_runtime.local_review_requires_agent),
        "accept-current-cross-owner-local-review": (cross_runtime.accept_local_review),
        "prepare-current-cross-owner-recheck": (cross_runtime.prepare_recheck),
        "cross-owner-recheck-requires-agent": (cross_runtime.recheck_requires_agent),
        "accept-current-cross-owner-recheck": (cross_runtime.accept_recheck),
        "advance-current-cross-owner-round": cross_runtime.advance_round,
        "cross-owner-round-needs-revision": (cross_runtime.round_needs_revision),
        "complete-current-cross-owner-pipeline": (cross_runtime.complete_owner_round),
        "complete-current-cross-owner-without-findings": (
            cross_runtime.complete_owner_without_findings
        ),
        "reduce-cross-owner-cohort": cross_runtime.reduce,
        "prepare-chief-chapter-cohort": prepare_chief_with_boundary,
        "prepare-current-chief-chapter": chief_runtime.prepare_lane,
        "chief-chapter-requires-agent": chief_runtime.requires_agent,
        "accept-current-chief-chapter": chief_runtime.accept_lane,
        "complete-current-chief-chapter": chief_runtime.complete_lane,
        "reduce-chief-chapter-cohort": chief_runtime.reduce,
        "prepare-final-chapter-cohort": prepare_final_with_boundary,
        "prepare-current-final-chapter": final_runtime.prepare_lane,
        "final-chapter-initial-requires-agent": (final_runtime.requires_agent),
        "accept-current-final-chapter-initial": final_runtime.accept_lane,
        "complete-current-final-chapter": final_runtime.complete_lane,
        "reduce-final-chapter-cohort": final_runtime.reduce,
        "start-final-review-cycle": final_review_runtime.start_cycle,
        "final-review-needs-round": final_review_runtime.needs_round,
        "advance-final-review-round": final_review_runtime.advance_round,
        "prepare-current-final-chief-revision": (final_review_runtime.prepare_chief_revision),
        "final-chief-revision-requires-agent": (final_review_runtime.chief_revision_requires_agent),
        "accept-current-final-chief-revision": (final_review_runtime.accept_chief_revision),
        "complete-current-final-chief-revision": (final_review_runtime.complete_chief_revision),
        "reduce-final-chief-revision-cohort": (final_review_runtime.reduce_chief_revisions),
        "prepare-current-final-recheck": (final_review_runtime.prepare_recheck),
        "final-recheck-requires-agent": (final_review_runtime.recheck_requires_agent),
        "accept-current-final-recheck": (final_review_runtime.accept_recheck),
        "complete-current-final-recheck": (final_review_runtime.complete_recheck),
        "reduce-final-recheck-cohort": (final_review_runtime.reduce_rechecks),
        "complete-final-review": final_review_runtime.complete_review,
        "prepare-render-delivery": prepare_delivery_with_boundary,
        "publish-materialize-delivery": delivery_runtime.publish,
        "complete-delivery": delivery_runtime.complete,
    }
    runtime_tools = _assemble_reporting_capability_tools(
        definitions,
        contracts,
        runtime_tools,
        plan,
        cohort_plan,
        cross_cohort_plan,
        chief_cohort_plan,
        final_cohort_plan,
        *subworkflows.values(),
    )
    try:
        completed = await WorkflowRuntimeHost(
            executors,
            state_store,
            event_sink if event_sink is not None else InMemoryWorkflowEventSink(),
        ).execute(
            plan,
            kernel_state,
            RuntimeContext(
                tools=runtime_tools,
                agents=compose_final_review_agent_invokers(
                    _reporting_agent_invokers(
                        module_runtime.agent_invokers,
                        cross_runtime.agent_invokers,
                        chief_runtime.agent_invokers,
                        final_runtime.agent_invokers,
                    ),
                    final_review_runtime,
                ),
                contracts=contracts,
                definitions=definitions,
                conversations=(
                    ConversationRegistry(FileConversationStore(workspace))
                    if workspace is not None
                    else ConversationRegistry()
                ),
                subworkflows=subworkflows,
            ),
        )
    except BaseException:
        current = (
            delivery_runtime.current_state
            or tail_adapters.current_state
            or final_review_runtime.current_state
            or final_runtime.current_state
            or chief_runtime.current_state
            or cross_runtime.current_state
            or module_runtime.current_state
            or state
        )
        current = deepcopy(current)
        state.clear()
        state.update(current)
        raise
    if completed.status is WorkflowStatus.WAITING:
        current = (
            delivery_runtime.current_state
            or tail_adapters.current_state
            or final_review_runtime.current_state
            or final_runtime.current_state
            or chief_runtime.current_state
            or cross_runtime.current_state
            or module_runtime.current_state
            or state
        )
        current = deepcopy(current)
        state.clear()
        state.update(current)
        waiting = completed.waiting_input or {}
        reason = str(
            waiting.get("description")
            or f"workflow is waiting for input: {waiting.get('input_id', 'unknown')}"
        )
        raise ReportingNeedsDecisionError(reason)
    result = deepcopy(completed.outputs["result"])
    state.clear()
    state.update(result)
    return completed


class _BatchModuleRuntime:
    """Compatibility adapter for callers that still provide one batch callback."""

    def __init__(
        self,
        execute_current: Callable[[tuple[str, ...], dict[str, Any], str], Awaitable[None]],
        requested_modules: tuple[str, ...],
        state: dict[str, Any],
        workflow_id: str,
    ) -> None:
        self._execute_current = execute_current
        self._requested_modules = requested_modules
        self.current_state = deepcopy(state)
        self._workflow_id = workflow_id
        self.agent_invokers: Mapping[str, AgentInvoker] = {}

    async def prepare_lanes(self, state: dict[str, Any]) -> dict[str, Any]:
        return state

    async def start_lane(
        self,
        module_id: str,
        state: dict[str, Any],
        workflow_id: str,
        lane_outcome: DeclarativeModuleLaneOutcome | None = None,
    ) -> DeclarativeModuleRuntimeLaneContext:
        del lane_outcome
        return DeclarativeModuleRuntimeLaneContext(
            module_id=module_id,
            workflow_id=workflow_id,
            reporting_state=state,
            status="completed",
        )

    async def prepare_author_lane(
        self,
        context: DeclarativeModuleRuntimeLaneContext,
    ) -> DeclarativeModuleRuntimeLaneContext:
        return context

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
        return context

    async def can_review_lane(
        self,
        _context: DeclarativeModuleRuntimeLaneContext,
    ) -> bool:
        return False

    async def prepare_review_lane(
        self,
        context: DeclarativeModuleRuntimeLaneContext,
    ) -> DeclarativeModuleRuntimeLaneContext:
        return context

    async def review_preflight_needs_revision(
        self,
        _context: DeclarativeModuleRuntimeLaneContext,
    ) -> bool:
        return False

    async def prepare_preflight_revision_lane(
        self,
        context: DeclarativeModuleRuntimeLaneContext,
    ) -> DeclarativeModuleRuntimeLaneContext:
        return context

    async def accept_preflight_revision_lane(
        self,
        values: Mapping[str, Any],
    ) -> DeclarativeModuleRuntimeLaneContext:
        return DeclarativeModuleRuntimeLaneContext.model_validate(values["context"])

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

    async def review_needs_recheck(
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
        return context

    async def resume_recheck_lane(
        self,
        context: DeclarativeModuleRuntimeLaneContext,
    ) -> DeclarativeModuleRuntimeLaneContext:
        return context

    async def lane_has_deferred_main_exception(
        self,
        _context: DeclarativeModuleRuntimeLaneContext,
    ) -> bool:
        return False

    async def lane_retries_preflight_revision(
        self,
        _context: DeclarativeModuleRuntimeLaneContext,
    ) -> bool:
        return False

    async def preflight_revision_needs_recheck(
        self,
        _context: DeclarativeModuleRuntimeLaneContext,
    ) -> bool:
        return False

    async def prepare_main_exception_lane(
        self,
        context: DeclarativeModuleRuntimeLaneContext,
    ) -> DeclarativeModuleRuntimeLaneContext:
        return context

    async def main_exception_requires_agent(
        self,
        _context: DeclarativeModuleRuntimeLaneContext,
    ) -> bool:
        return False

    async def accept_main_exception_lane(
        self,
        values: Mapping[str, Any],
    ) -> DeclarativeModuleRuntimeLaneContext:
        return DeclarativeModuleRuntimeLaneContext.model_validate(values["context"])

    async def main_exception_requests_user(
        self,
        _context: DeclarativeModuleRuntimeLaneContext,
    ) -> bool:
        return False

    async def apply_main_exception_user_input(
        self,
        values: Mapping[str, Any],
    ) -> DeclarativeModuleRuntimeLaneContext:
        return DeclarativeModuleRuntimeLaneContext.model_validate(values["context"])

    async def route_after_main_exception(
        self,
        context: DeclarativeModuleRuntimeLaneContext,
    ) -> DeclarativeModuleRuntimeLaneContext:
        return context

    async def complete_lane(
        self,
        context: DeclarativeModuleRuntimeLaneContext,
    ) -> DeclarativeModuleLaneOutcome:
        return DeclarativeModuleLaneOutcome(
            module_id=context.module_id,
            status="completed",
        )

    async def reduce_lanes(
        self,
        _outcomes: Mapping[str, Any],
    ) -> dict[str, Any]:
        await self._execute_current(
            self._requested_modules,
            self.current_state,
            self._workflow_id,
        )
        return self.current_state


class DeclarativeReportWorkflowRunner(ReportWorkflowRunner):
    """Select declarative stage orchestration without replacing current semantics."""

    _BOUNDARIES_KEY = "_declarative_cost_boundaries"

    def __init__(self, service, agent_runner) -> None:
        super().__init__(service, agent_runner)

    async def _agent(
        self,
        agent_id: str,
        envelope: TaskEnvelope,
        artifacts: list[str],
        workflow_id: str,
        *,
        session_key: str | None = None,
        recovery_policy: RecoveryPolicyDefinition | None = None,
        definition_override=None,
    ):
        return await super()._agent(
            agent_id,
            envelope,
            artifacts,
            workflow_id,
            session_key=session_key,
            recovery_policy=recovery_policy,
            definition_override=definition_override,
        )

    async def _run_module_lanes(
        self,
        requested_modules: tuple[str, ...],
        state: dict,
        workflow_id: str,
    ) -> None:
        async def stage_boundary(
            current_state: dict[str, Any],
            completed_stage: str,
            next_stage: str,
        ) -> None:
            state.clear()
            state.update(deepcopy(current_state))
            await self._declarative_stage_boundary(
                state,
                completed_stage,
                next_stage,
            )

        await execute_declarative_module_stage(
            requested_modules=requested_modules,
            state=state,
            workflow_id=workflow_id,
            state_store=FileWorkflowStateStore(self.service.workspace),
            tail_runner=_CurrentTailStages(self),
            event_sink=FileWorkflowEventSink(self.service.workspace),
            module_runtime=_CurrentModuleStages(
                self,
                requested_modules,
                state,
                workflow_id,
            ),
            workspace=self.service.workspace,
            stage_boundary=stage_boundary,
        )

    @classmethod
    def _boundary_marker(cls, completed_stage: str, next_stage: str | None) -> str:
        return f"{completed_stage}->{next_stage or ''}"

    async def _declarative_stage_boundary(
        self,
        state: dict[str, Any],
        completed_stage: str,
        next_stage: str,
    ) -> None:
        marker = self._boundary_marker(completed_stage, next_stage)
        completed = list(state.get(self._BOUNDARIES_KEY, ()))
        if marker in completed:
            return
        state[self._BOUNDARIES_KEY] = [*completed, marker]
        await super()._checkpoint_then_cost_boundary(
            state,
            completed_stage,
            "completed",
            completed_stage,
            next_stage,
        )

    async def _checkpoint_then_cost_boundary(
        self,
        state: dict,
        activity: str,
        status: str,
        completed_stage: str,
        next_stage: str | None,
        *,
        checkpoint_kind: str = "full",
    ) -> None:
        marker = self._boundary_marker(completed_stage, next_stage)
        if marker in state.get(self._BOUNDARIES_KEY, ()):
            return
        await super()._checkpoint_then_cost_boundary(
            state,
            activity,
            status,
            completed_stage,
            next_stage,
            checkpoint_kind=checkpoint_kind,
        )


class _CurrentModuleAuthorInvoker:
    """Invoke the current Reporting Agent behind the generic Agent port."""

    def __init__(
        self,
        runner: DeclarativeReportWorkflowRunner,
        capture_failure: Callable[[str, BaseException], None],
    ) -> None:
        self._runner = runner
        self._capture_failure = capture_failure

    async def invoke(
        self,
        agent: AgentDefinition,
        task: TaskDefinition,
        value: Any,
        conversation: ConversationRecord,
        *,
        task_id: str,
    ) -> AgentInvocationOutcome:
        return await self._invoke(
            agent,
            task,
            value,
            conversation,
            task_id=task_id,
            recovery_policy=None,
        )

    async def invoke_with_recovery(
        self,
        agent: AgentDefinition,
        task: TaskDefinition,
        value: Any,
        conversation: ConversationRecord,
        *,
        task_id: str,
        recovery_policy: RecoveryPolicyDefinition,
    ) -> AgentInvocationOutcome:
        return await self._invoke(
            agent,
            task,
            value,
            conversation,
            task_id=task_id,
            recovery_policy=recovery_policy,
        )

    async def _invoke(
        self,
        agent: AgentDefinition,
        task: TaskDefinition,
        value: Any,
        conversation: ConversationRecord,
        *,
        task_id: str,
        recovery_policy: RecoveryPolicyDefinition | None,
    ) -> AgentInvocationOutcome:
        del task_id
        context = DeclarativeModuleRuntimeLaneContext.model_validate(value)
        if context.revision is not None:
            preparation = context.revision.prepared
            try:
                runner_kwargs: dict[str, Any] = {
                    "session_key": conversation.key.value,
                    "definition_override": project_reporting_agent(agent),
                }
                if recovery_policy is not None:
                    runner_kwargs["recovery_policy"] = recovery_policy
                envelope = bind_declared_task(preparation.envelope, task)
                payload = await self._runner._agent(
                    preparation.specialist_id,
                    envelope,
                    envelope.input_refs,
                    context.workflow_id,
                    **runner_kwargs,
                )
                result = DeclarativeModuleRevisionAgentResult(
                    status="completed",
                    submission=payload,
                )
            except asyncio.CancelledError:
                raise
            except BaseException as exc:
                self._capture_failure(context.module_id, exc)
                result = DeclarativeModuleRevisionAgentResult(
                    status="failed",
                    error=str(exc),
                )
            return AgentInvocationOutcome(status="ok", result=result)
        authoring = cast(
            DeclarativeModuleAuthoringPreparation,
            context.authoring,
        )
        envelope = bind_declared_task(authoring.envelope, task)
        try:
            runner_kwargs = {
                "session_key": conversation.key.value,
                "definition_override": project_reporting_agent(agent),
            }
            if recovery_policy is not None:
                runner_kwargs["recovery_policy"] = recovery_policy
            payload = await self._runner._agent(
                authoring.specialist_id,
                envelope,
                envelope.input_refs,
                context.workflow_id,
                **runner_kwargs,
            )
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            self._capture_failure(context.module_id, exc)
            result = DeclarativeModuleAuthoringAgentResult(
                status="failed",
                error=str(exc),
            )
        else:
            result = DeclarativeModuleAuthoringAgentResult(
                status="completed",
                module=payload,
            )
        return AgentInvocationOutcome(status="ok", result=result)


class _CurrentModuleReviewerInvoker:
    """Invoke the current initial Auditor behind the generic Agent port."""

    def __init__(
        self,
        runner: DeclarativeReportWorkflowRunner,
        capture_failure: Callable[[str, BaseException], None],
    ) -> None:
        self._runner = runner
        self._capture_failure = capture_failure

    async def invoke(
        self,
        agent: AgentDefinition,
        task: TaskDefinition,
        value: Any,
        conversation: ConversationRecord,
        *,
        task_id: str,
    ) -> AgentInvocationOutcome:
        return await self._invoke(
            agent,
            task,
            value,
            conversation,
            task_id=task_id,
            recovery_policy=None,
        )

    async def invoke_with_recovery(
        self,
        agent: AgentDefinition,
        task: TaskDefinition,
        value: Any,
        conversation: ConversationRecord,
        *,
        task_id: str,
        recovery_policy: RecoveryPolicyDefinition,
    ) -> AgentInvocationOutcome:
        return await self._invoke(
            agent,
            task,
            value,
            conversation,
            task_id=task_id,
            recovery_policy=recovery_policy,
        )

    async def _invoke(
        self,
        agent: AgentDefinition,
        task: TaskDefinition,
        value: Any,
        conversation: ConversationRecord,
        *,
        task_id: str,
        recovery_policy: RecoveryPolicyDefinition | None,
    ) -> AgentInvocationOutcome:
        del task_id
        context = DeclarativeModuleRuntimeLaneContext.model_validate(value)
        if context.recheck is not None:
            preparation = context.recheck.prepared
            envelope = bind_declared_task(
                cast(TaskEnvelope, preparation.envelope),
                task,
            )
            try:
                runner_kwargs: dict[str, Any] = {
                    "session_key": conversation.key.value,
                    "definition_override": project_reporting_agent(agent),
                }
                if recovery_policy is not None:
                    runner_kwargs["recovery_policy"] = recovery_policy
                payload = await self._runner._agent(
                    "evidence-auditor",
                    envelope,
                    envelope.input_refs,
                    context.workflow_id,
                    **runner_kwargs,
                )
                result = DeclarativeModuleRecheckAgentResult(
                    status="completed",
                    submission=payload,
                )
            except asyncio.CancelledError:
                raise
            except BaseException as exc:
                self._capture_failure(context.module_id, exc)
                result = DeclarativeModuleRecheckAgentResult(
                    status="failed",
                    error=str(exc),
                )
            return AgentInvocationOutcome(status="ok", result=result)
        reviewing = cast(DeclarativeModuleReviewPreparation, context.review)
        envelope = bind_declared_task(
            cast(TaskEnvelope, reviewing.prepared.envelope),
            task,
        )
        try:
            runner_kwargs = {
                "session_key": conversation.key.value,
                "definition_override": project_reporting_agent(agent),
            }
            if recovery_policy is not None:
                runner_kwargs["recovery_policy"] = recovery_policy
            payload = await self._runner._agent(
                "evidence-auditor",
                envelope,
                envelope.input_refs,
                context.workflow_id,
                **runner_kwargs,
            )
            result = DeclarativeModuleReviewAgentResult(
                status="completed",
                submission=payload,
            )
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            self._capture_failure(context.module_id, exc)
            result = DeclarativeModuleReviewAgentResult(
                status="failed",
                error=str(exc),
            )
        return AgentInvocationOutcome(status="ok", result=result)


class _CurrentModuleMainExceptionInvoker:
    """Invoke the existing serialized module Main exception through the Agent port."""

    def __init__(
        self,
        runner: DeclarativeReportWorkflowRunner,
        capture_failure: Callable[[str, BaseException], None],
    ) -> None:
        self._runner = runner
        self._capture_failure = capture_failure

    async def invoke(
        self,
        agent: AgentDefinition,
        task: TaskDefinition,
        value: Any,
        conversation: ConversationRecord,
        *,
        task_id: str,
    ) -> AgentInvocationOutcome:
        return await self._invoke(
            agent,
            task,
            value,
            conversation,
            task_id=task_id,
            recovery_policy=None,
        )

    async def invoke_with_recovery(
        self,
        agent: AgentDefinition,
        task: TaskDefinition,
        value: Any,
        conversation: ConversationRecord,
        *,
        task_id: str,
        recovery_policy: RecoveryPolicyDefinition,
    ) -> AgentInvocationOutcome:
        return await self._invoke(
            agent,
            task,
            value,
            conversation,
            task_id=task_id,
            recovery_policy=recovery_policy,
        )

    async def _invoke(
        self,
        agent: AgentDefinition,
        task: TaskDefinition,
        value: Any,
        conversation: ConversationRecord,
        *,
        task_id: str,
        recovery_policy: RecoveryPolicyDefinition | None,
    ) -> AgentInvocationOutcome:
        del task_id
        context = DeclarativeModuleRuntimeLaneContext.model_validate(value)
        preparation = cast(MainExceptionDecisionPreparation, context.main_preparation)

        async def invoke_once() -> Any:
            envelope = bind_declared_task(preparation.envelope, task)
            runner_kwargs: dict[str, Any] = {
                "session_key": conversation.key.value,
                "definition_override": project_reporting_agent(agent),
            }
            if recovery_policy is not None:
                runner_kwargs["recovery_policy"] = recovery_policy
            return await self._runner._agent(
                "main-agent",
                envelope,
                envelope.input_refs,
                preparation.workflow_id,
                **runner_kwargs,
            )

        try:
            lock = getattr(self._runner, "_main_exception_lock", None)
            if lock is None:
                payload = await invoke_once()
            else:
                async with lock:
                    payload = await invoke_once()
            result = DeclarativeMainExceptionAgentResult(
                status="completed",
                submission=payload,
            )
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            self._capture_failure(context.module_id, exc)
            result = DeclarativeMainExceptionAgentResult(
                status="failed",
                error=str(exc),
            )
        return AgentInvocationOutcome(status="ok", result=result)


class _CurrentModuleStages:
    """Bind complete current Lane semantics to file-defined Cohort branches."""

    def __init__(
        self,
        runner: DeclarativeReportWorkflowRunner,
        requested_modules: tuple[str, ...],
        state: dict[str, Any],
        workflow_id: str,
    ) -> None:
        self._runner = runner
        self._requested_modules = requested_modules
        self.current_state = deepcopy(state)
        self._workflow_id = workflow_id
        self._failures: dict[str, BaseException] = {}
        self._author_failures: dict[str, BaseException] = {}
        self._review_failures: dict[str, BaseException] = {}
        self._main_failures: dict[str, BaseException] = {}
        author_invoker = _CurrentModuleAuthorInvoker(
            runner,
            self._capture_author_failure,
        )
        reviewer_invoker = _CurrentModuleReviewerInvoker(
            runner,
            self._capture_review_failure,
        )
        main_invoker = _CurrentModuleMainExceptionInvoker(
            runner,
            self._capture_main_failure,
        )
        self.agent_invokers: Mapping[str, AgentInvoker] = {
            **{f"module-{module_id}-specialist": author_invoker for module_id in REPORT_MODULE_IDS},
            "evidence-auditor": reviewer_invoker,
            "main-agent": main_invoker,
        }

    async def prepare_lanes(self, state: dict[str, Any]) -> dict[str, Any]:
        return state

    async def start_lane(
        self,
        module_id: str,
        lane_state: dict[str, Any],
        workflow_id: str,
        lane_outcome: DeclarativeModuleLaneOutcome | None = None,
    ) -> DeclarativeModuleRuntimeLaneContext:
        if lane_outcome is not None:
            saved_context = (lane_outcome.lane_state or {}).get("lane_context")
            if saved_context is not None and (
                lane_outcome.status == "deferred" or lane_outcome.retry_requested
            ):
                restored = DeclarativeModuleRuntimeLaneContext.model_validate(saved_context)
                reporting_state = deepcopy(restored.reporting_state)
                reporting_state["resume"] = True
                reporting_state["_defer_main_exceptions"] = False
                status = (
                    restored.resume_status
                    if lane_outcome.status == "failed" and restored.resume_status is not None
                    else restored.status
                )
                return restored.model_copy(
                    deep=True,
                    update={
                        "reporting_state": reporting_state,
                        "status": status,
                        "resume_status": None,
                        "error": None,
                    },
                )
            return DeclarativeModuleRuntimeLaneContext(
                module_id=module_id,
                workflow_id=workflow_id,
                reporting_state=deepcopy(
                    (lane_outcome.lane_state or {}).get(
                        "reporting_state",
                        lane_state,
                    )
                ),
                status=("completed" if lane_outcome.status == "completed" else "failed"),
                module=lane_outcome.module,
                completion_ref=lane_outcome.completion_ref,
                completion=(
                    LaneCompletion.model_validate(lane_outcome.completion)
                    if lane_outcome.completion is not None
                    else None
                ),
                error=lane_outcome.error,
            )
        if module_id not in self._requested_modules:
            return DeclarativeModuleRuntimeLaneContext(
                module_id=module_id,
                workflow_id=workflow_id,
                reporting_state=deepcopy(lane_state),
                status="completed",
            )
        try:
            self._runner._raise_if_cancel_requested(lane_state["run_id"])
            recovered_lanes = self._runner._recovery_store(lane_state).load_completed_lanes(
                self._runner._recovery_stage_name("module"),
                [module_id],
            )
            recovered = None
            if module_id in recovered_lanes:
                recovered = self._runner._load_recovery_module_lane(
                    module_id,
                    lane_state,
                    recovered_lanes[module_id],
                )
            if recovered is None and module_id not in lane_state.get(
                "module_submissions",
                {},
            ):
                attempt = self._runner._start_module_lane_attempt(
                    module_id,
                    lane_state,
                    workflow_id,
                    True,
                    None,
                )
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            self._failures[module_id] = exc
            return DeclarativeModuleRuntimeLaneContext(
                module_id=module_id,
                workflow_id=workflow_id,
                reporting_state=deepcopy(lane_state),
                status="failed",
                error=str(exc),
            )
        if recovered is not None:
            submission, completion_ref, completion, completed_lane_state = recovered
            return DeclarativeModuleRuntimeLaneContext(
                module_id=module_id,
                workflow_id=workflow_id,
                reporting_state=completed_lane_state,
                status="completed",
                module=submission,
                completion_ref=completion_ref,
                completion=completion,
            )
        if module_id in lane_state.get("module_submissions", {}):
            return DeclarativeModuleRuntimeLaneContext(
                module_id=module_id,
                workflow_id=workflow_id,
                reporting_state=lane_state,
                status="completed",
                module=lane_state["module_submissions"][module_id],
            )
        return DeclarativeModuleRuntimeLaneContext(
            module_id=module_id,
            workflow_id=workflow_id,
            reporting_state=attempt.lane_state,
            status="ready",
            attempt=DeclarativeModuleLaneAttempt(
                spec=attempt.spec,
                spec_ref=attempt.spec_ref,
                lane_attempt_id=attempt.lane_attempt_id,
                started_at_ns=attempt.started_at_ns,
                attempt_ref=attempt.attempt_ref,
            ),
        )

    async def prepare_author_lane(
        self,
        context: DeclarativeModuleRuntimeLaneContext,
    ) -> DeclarativeModuleRuntimeLaneContext:
        if context.status != "ready":
            return context
        try:
            preparation = self._runner._prepare_module_authoring(
                context.module_id,
                context.reporting_state,
                context.workflow_id,
                review=False,
                checkpoint=False,
            )
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            return self._failed_lane_context(context, exc)
        return context.model_copy(
            deep=True,
            update={
                "status": (
                    "author_resumed" if preparation.resumed_payload is not None else "author_ready"
                ),
                "authoring": DeclarativeModuleAuthoringPreparation(
                    specialist_id=preparation.specialist_id,
                    envelope=preparation.envelope,
                    resumed_payload=preparation.resumed_payload,
                    revision=preparation.revision,
                    review=preparation.review,
                    checkpoint=preparation.checkpoint,
                ),
            },
        )

    async def prepare_author_exception_lane(
        self,
        context: DeclarativeModuleRuntimeLaneContext,
    ) -> DeclarativeModuleRuntimeLaneContext:
        if context.status != "recheck_pending" or context.module is None:
            return context
        exceptional = [
            response
            for response in context.module.revision_responses
            if response.action in {"disputed", "needs_input"}
        ]
        if not exceptional:
            return context
        return context.model_copy(
            deep=True,
            update={"status": "author_exception_deferred"},
        )

    async def author_requires_agent(
        self,
        context: DeclarativeModuleRuntimeLaneContext,
    ) -> bool:
        return context.status == "author_ready"

    async def accept_author_lane(
        self,
        values: Mapping[str, Any],
    ) -> DeclarativeModuleRuntimeLaneContext:
        context = DeclarativeModuleRuntimeLaneContext.model_validate(values["context"])
        result = DeclarativeModuleAuthoringAgentResult.model_validate(values["result"])
        if result.status == "failed":
            exc = self._author_failures.pop(
                context.module_id,
                AgentWorkflowError(result.error or "module author failed"),
            )
            return self._failed_lane_context(context, exc)
        try:
            submission = self._runner._accept_module_authoring(
                self._restore_authoring(context),
                result.module,
            )
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            return self._failed_lane_context(context, exc)
        return context.model_copy(
            deep=True,
            update={
                "status": "authored",
                "authoring": None,
                "module": submission,
            },
        )

    async def resume_author_lane(
        self,
        context: DeclarativeModuleRuntimeLaneContext,
    ) -> DeclarativeModuleRuntimeLaneContext:
        if context.status != "author_resumed":
            return context
        try:
            submission = await self._runner._resume_module_authoring(
                self._restore_authoring(context)
            )
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            return self._failed_lane_context(context, exc)
        return context.model_copy(
            deep=True,
            update={
                "status": "authored",
                "authoring": None,
                "module": cast(Any, submission),
            },
        )

    def _capture_author_failure(
        self,
        module_id: str,
        exc: BaseException,
    ) -> None:
        self._author_failures[module_id] = exc

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
        previous_review = context.review
        preflight_progress = (
            previous_review.prepared.preflight_progress if previous_review is not None else None
        )
        try:
            preparation = await self._runner._prepare_module_initial_review_step(
                context.module_id,
                cast(Any, context.module),
                context.reporting_state,
                context.workflow_id,
                initial_scope=set(REPORT_TAXONOMY[context.module_id].submodules),
                preflight_progress=preflight_progress,
            )
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            return self._failed_lane_context(context, exc)
        return context.model_copy(
            deep=True,
            update={
                "status": (
                    "preflight_revision_pending"
                    if preparation.mode == "preflight_revision"
                    else (
                        "review_ready" if preparation.mode == "invoke_agent" else "review_resumed"
                    )
                ),
                "module": preparation.current,
                "review": DeclarativeModuleReviewPreparation(
                    envelope=preparation.envelope,
                    reviewer_session_key=preparation.reviewer_session_key,
                    prepared=preparation,
                ),
            },
        )

    @staticmethod
    async def review_preflight_needs_revision(
        context: DeclarativeModuleRuntimeLaneContext,
    ) -> bool:
        return context.status == "preflight_revision_pending"

    async def prepare_preflight_revision_lane(
        self,
        context: DeclarativeModuleRuntimeLaneContext,
    ) -> DeclarativeModuleRuntimeLaneContext:
        if context.status != "preflight_revision_pending":
            return context
        try:
            if (
                context.recheck is not None
                and context.recheck.prepared.mode == "preflight_revision"
            ):
                preparation = await self._runner._prepare_module_recheck_preflight_revision(
                    context.recheck.prepared,
                    context.reporting_state,
                )
            else:
                reviewing = cast(DeclarativeModuleReviewPreparation, context.review)
                preparation = await self._runner._prepare_module_initial_review_preflight_revision(
                    reviewing.prepared,
                    context.reporting_state,
                )
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            return self._failed_lane_context(context, exc)
        return context.model_copy(
            deep=True,
            update={
                "status": "preflight_revision_ready",
                "revision": DeclarativeModuleRevisionPreparation(
                    prepared=preparation,
                ),
            },
        )

    async def accept_preflight_revision_lane(
        self,
        values: Mapping[str, Any],
    ) -> DeclarativeModuleRuntimeLaneContext:
        context = DeclarativeModuleRuntimeLaneContext.model_validate(values["context"])
        result = DeclarativeModuleRevisionAgentResult.model_validate(values["result"])
        if result.status == "failed":
            exc = self._author_failures.pop(
                context.module_id,
                AgentWorkflowError(result.error or "module preflight revision failed"),
            )
            return self._failed_lane_context(context, exc)
        revision = cast(DeclarativeModuleRevisionPreparation, context.revision)
        try:
            if (
                context.recheck is not None
                and context.recheck.prepared.mode == "preflight_revision"
            ):
                revised, _subject_ref = self._runner._accept_module_recheck_preflight_revision(
                    context.recheck.prepared,
                    revision.prepared,
                    cast(Any, result.submission),
                    context.reporting_state,
                )
                next_status = "recheck_pending"
            else:
                reviewing = cast(DeclarativeModuleReviewPreparation, context.review)
                revised, _subject_ref = (
                    self._runner._accept_module_initial_review_preflight_revision(
                        reviewing.prepared,
                        revision.prepared,
                        cast(Any, result.submission),
                        context.reporting_state,
                    )
                )
                next_status = "authored"
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            return self._failed_lane_context(context, exc)
        return context.model_copy(
            deep=True,
            update={
                "status": next_status,
                "module": revised,
                "revision": None,
            },
        )

    async def review_requires_agent(
        self,
        context: DeclarativeModuleRuntimeLaneContext,
    ) -> bool:
        return context.status == "review_ready"

    async def accept_review_lane(
        self,
        values: Mapping[str, Any],
    ) -> DeclarativeModuleRuntimeLaneContext:
        context = DeclarativeModuleRuntimeLaneContext.model_validate(values["context"])
        result = DeclarativeModuleReviewAgentResult.model_validate(values["result"])
        if result.status == "failed":
            exc = self._review_failures.pop(
                context.module_id,
                AgentWorkflowError(result.error or "module reviewer failed"),
            )
            return self._failed_lane_context(context, exc)
        try:
            accepted = self._runner._accept_module_initial_review(
                cast(
                    DeclarativeModuleReviewPreparation,
                    context.review,
                ).prepared,
                cast(Any, result.submission),
                context.reporting_state,
            )
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            return self._failed_lane_context(context, exc)
        return context.model_copy(
            deep=True,
            update={
                "status": (
                    "reviewed" if accepted.next_action == "completed" else "revision_pending"
                ),
                "module": accepted.current,
                "review": cast(
                    DeclarativeModuleReviewPreparation,
                    context.review,
                ).model_copy(update={"acceptance": accepted}),
            },
        )

    async def resume_review_lane(
        self,
        context: DeclarativeModuleRuntimeLaneContext,
    ) -> DeclarativeModuleRuntimeLaneContext:
        if context.status != "review_resumed":
            return context
        try:
            reviewing = cast(DeclarativeModuleReviewPreparation, context.review)
            progress = reviewing.prepared.progress
            if (
                progress is not None
                and progress.next_action == "review"
                and progress.phase == "initial"
                and reviewing.envelope is not None
            ):
                return context.model_copy(
                    deep=True,
                    update={
                        "status": "review_ready",
                        "module": progress.current,
                        "review": reviewing.model_copy(
                            update={
                                "prepared": reviewing.prepared.model_copy(
                                    update={
                                        "mode": "invoke_agent",
                                        "current": progress.current,
                                    }
                                )
                            }
                        ),
                    },
                )
            accepted = self._runner._resume_module_initial_review(
                reviewing.prepared,
                context.reporting_state,
            )
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            return self._failed_lane_context(context, exc)
        return context.model_copy(
            deep=True,
            update={
                "status": (
                    "reviewed" if accepted.next_action == "completed" else "revision_pending"
                ),
                "module": accepted.current,
                "review": reviewing.model_copy(update={"acceptance": accepted}),
            },
        )

    async def resume_recheck_lane(
        self,
        context: DeclarativeModuleRuntimeLaneContext,
    ) -> DeclarativeModuleRuntimeLaneContext:
        if context.status != "review_resumed":
            return context
        rechecking = cast(DeclarativeModuleRecheckPreparation, context.recheck)
        progress = rechecking.prepared.progress
        if (
            progress is not None
            and progress.next_action == "review"
            and progress.phase == "recheck"
            and rechecking.prepared.envelope is not None
        ):
            return context.model_copy(
                deep=True,
                update={
                    "status": "recheck_ready",
                    "module": progress.current,
                    "recheck": rechecking.model_copy(
                        update={
                            "prepared": rechecking.prepared.model_copy(
                                update={
                                    "mode": "invoke_agent",
                                    "current": progress.current,
                                }
                            )
                        }
                    ),
                },
            )
        if progress is not None and progress.next_action == "completed":
            try:
                accepted = self._runner._resume_module_recheck(
                    rechecking.prepared,
                    context.reporting_state,
                )
            except asyncio.CancelledError:
                raise
            except BaseException as exc:
                return self._failed_lane_context(context, exc)
            return context.model_copy(
                deep=True,
                update={
                    "status": "reviewed",
                    "module": accepted.current,
                    "review": cast(
                        DeclarativeModuleReviewPreparation,
                        context.review,
                    ).model_copy(update={"acceptance": accepted}),
                    "recheck": rechecking,
                },
            )
        return self._failed_lane_context(
            context,
            ReviewLifecycleError("module recheck continuation has no declared recovery state"),
        )

    async def review_needs_revision(
        self,
        context: DeclarativeModuleRuntimeLaneContext,
    ) -> bool:
        return context.status == "revision_pending"

    async def review_needs_recheck(
        self,
        context: DeclarativeModuleRuntimeLaneContext,
    ) -> bool:
        return context.status == "recheck_pending"

    async def prepare_revision_lane(
        self,
        context: DeclarativeModuleRuntimeLaneContext,
    ) -> DeclarativeModuleRuntimeLaneContext:
        if context.status != "revision_pending":
            return context
        reviewing = cast(DeclarativeModuleReviewPreparation, context.review)
        acceptance = cast(Any, reviewing.acceptance)
        try:
            preparation = await self._runner._prepare_module_revision(
                acceptance.current,
                context.reporting_state,
                context.workflow_id,
                acceptance.findings,
            )
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            return self._failed_lane_context(context, exc)
        return context.model_copy(
            deep=True,
            update={
                "status": "revision_ready",
                "revision": DeclarativeModuleRevisionPreparation(
                    prepared=preparation,
                ),
            },
        )

    async def accept_revision_lane(
        self,
        values: Mapping[str, Any],
    ) -> DeclarativeModuleRuntimeLaneContext:
        context = DeclarativeModuleRuntimeLaneContext.model_validate(values["context"])
        result = DeclarativeModuleRevisionAgentResult.model_validate(values["result"])
        if result.status == "failed":
            exc = self._author_failures.pop(
                context.module_id,
                AgentWorkflowError(result.error or "module revision failed"),
            )
            return self._failed_lane_context(context, exc)
        try:
            revised, _subject_ref = self._runner._accept_module_revision(
                cast(DeclarativeModuleRevisionPreparation, context.revision).prepared,
                cast(Any, result.submission),
            )
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            return self._failed_lane_context(context, exc)
        return context.model_copy(
            deep=True,
            update={
                "status": "recheck_pending",
                "module": revised,
                "revision": None,
            },
        )

    async def prepare_recheck_lane(
        self,
        context: DeclarativeModuleRuntimeLaneContext,
    ) -> DeclarativeModuleRuntimeLaneContext:
        if context.status != "recheck_pending":
            return context
        previous_recheck = context.recheck
        preflight_progress = (
            previous_recheck.prepared.preflight_progress if previous_recheck is not None else None
        )
        try:
            preparation = await self._runner._prepare_module_recheck(
                context.module_id,
                cast(Any, context.module),
                context.reporting_state,
                context.workflow_id,
                initial_scope=set(REPORT_TAXONOMY[context.module_id].submodules),
                lifecycle_id="initial",
                preflight_progress=preflight_progress,
                author_exception_acceptance=context.main_acceptance,
            )
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            return self._failed_lane_context(context, exc)
        return context.model_copy(
            deep=True,
            update={
                "status": (
                    "preflight_revision_pending"
                    if preparation.mode == "preflight_revision"
                    else (
                        "recheck_ready" if preparation.mode == "invoke_agent" else "review_resumed"
                    )
                ),
                "module": preparation.current,
                "recheck": DeclarativeModuleRecheckPreparation(
                    prepared=preparation,
                ),
            },
        )

    async def recheck_requires_agent(
        self,
        context: DeclarativeModuleRuntimeLaneContext,
    ) -> bool:
        return context.status == "recheck_ready"

    async def accept_recheck_lane(
        self,
        values: Mapping[str, Any],
    ) -> DeclarativeModuleRuntimeLaneContext:
        context = DeclarativeModuleRuntimeLaneContext.model_validate(values["context"])
        result = DeclarativeModuleRecheckAgentResult.model_validate(values["result"])
        if result.status == "failed":
            exc = self._review_failures.pop(
                context.module_id,
                AgentWorkflowError(result.error or "module recheck failed"),
            )
            return self._failed_lane_context(context, exc)
        rechecking = cast(DeclarativeModuleRecheckPreparation, context.recheck)
        rechecking = rechecking.model_copy(update={"submission": result.submission})
        context = context.model_copy(deep=True, update={"recheck": rechecking})
        escalated = bool(
            result.submission is not None
            and any(verdict.verdict == "escalate" for verdict in result.submission.verdicts)
        )
        try:
            accepted = await self._runner._accept_module_recheck(
                rechecking.prepared,
                cast(Any, result.submission),
                context.reporting_state,
            )
        except asyncio.CancelledError:
            raise
        except DeferredMainDecision:
            return context.model_copy(
                deep=True,
                update={"status": "reviewer_exception_deferred"},
            )
        except BaseException as exc:
            return self._failed_lane_context(context, exc)
        if escalated:
            return context.model_copy(
                deep=True,
                update={
                    "status": "reviewer_exception_deferred",
                    "recheck": rechecking.model_copy(update={"acceptance": accepted}),
                },
            )
        return context.model_copy(
            deep=True,
            update={
                "status": (
                    "reviewed" if accepted.next_action == "completed" else "revision_pending"
                ),
                "module": accepted.current,
                "review": cast(
                    DeclarativeModuleReviewPreparation,
                    context.review,
                ).model_copy(update={"acceptance": accepted}),
                "recheck": None,
            },
        )

    @staticmethod
    async def lane_has_deferred_main_exception(
        context: DeclarativeModuleRuntimeLaneContext,
    ) -> bool:
        return context.status in {
            "author_exception_deferred",
            "reviewer_exception_deferred",
        }

    @staticmethod
    async def lane_retries_preflight_revision(
        context: DeclarativeModuleRuntimeLaneContext,
    ) -> bool:
        return context.status == "preflight_revision_ready"

    @staticmethod
    async def preflight_revision_needs_recheck(
        context: DeclarativeModuleRuntimeLaneContext,
    ) -> bool:
        return context.status == "recheck_pending"

    async def prepare_main_exception_lane(
        self,
        context: DeclarativeModuleRuntimeLaneContext,
    ) -> DeclarativeModuleRuntimeLaneContext:
        if context.status not in {
            "author_exception_deferred",
            "reviewer_exception_deferred",
        }:
            return context
        try:
            if context.status == "author_exception_deferred":
                reviewing = cast(
                    DeclarativeModuleReviewPreparation,
                    context.review,
                )
                acceptance = cast(Any, reviewing.acceptance)
                preparation = prepare_main_exception_decision(
                    self._runner,
                    state=context.reporting_state,
                    workflow_id=context.workflow_id,
                    scope="module",
                    subject_refs=[
                        f"Work/runs/{context.reporting_state['run_id']}/modules/"
                        f"{context.module_id}-r{cast(Any, context.module).revision}.json"
                    ],
                    finding_refs=list(acceptance.finding_refs),
                    verdicts=[],
                    responses=[
                        response
                        for response in cast(Any, context.module).revision_responses
                        if response.action in {"disputed", "needs_input"}
                    ],
                    trigger="author_response",
                )
                ready_status = "author_exception_ready"
                resumed_status = "author_exception_resumed"
            else:
                rechecking = cast(
                    DeclarativeModuleRecheckPreparation,
                    context.recheck,
                )
                submission = cast(Any, rechecking.submission)
                preparation = prepare_main_exception_decision(
                    self._runner,
                    state=context.reporting_state,
                    workflow_id=context.workflow_id,
                    scope="module",
                    subject_refs=[cast(str, rechecking.prepared.subject_ref)],
                    finding_refs=list(rechecking.prepared.finding_refs),
                    verdicts=[
                        verdict for verdict in submission.verdicts if verdict.verdict == "escalate"
                    ],
                    responses=list(rechecking.prepared.responses),
                )
                ready_status = "reviewer_exception_ready"
                resumed_status = "reviewer_exception_resumed"
            if preparation.mode == "continue_existing":
                acceptance = accept_main_exception_decision(
                    self._runner,
                    state=context.reporting_state,
                    preparation=preparation,
                    result=None,
                    raise_for_terminal_decisions=False,
                )
                if acceptance.result.decision == "stop_incomplete":
                    return context.model_copy(
                        deep=True,
                        update={
                            "status": "failed",
                            "main_preparation": preparation,
                            "main_acceptance": acceptance,
                            "error": acceptance.result.rationale,
                        },
                    )
                return context.model_copy(
                    deep=True,
                    update={
                        "status": resumed_status,
                        "main_preparation": preparation,
                        "main_acceptance": acceptance,
                    },
                )
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            return self._failed_lane_context(context, exc)
        return context.model_copy(
            deep=True,
            update={
                "status": ready_status,
                "main_preparation": preparation,
                "main_acceptance": None,
            },
        )

    @staticmethod
    async def main_exception_requires_agent(
        context: DeclarativeModuleRuntimeLaneContext,
    ) -> bool:
        return context.status in {
            "author_exception_ready",
            "reviewer_exception_ready",
        }

    async def accept_main_exception_lane(
        self,
        values: Mapping[str, Any],
    ) -> DeclarativeModuleRuntimeLaneContext:
        context = DeclarativeModuleRuntimeLaneContext.model_validate(values["context"])
        result = DeclarativeMainExceptionAgentResult.model_validate(values["result"])
        if result.status == "failed":
            exc = self._main_failures.pop(
                context.module_id,
                AgentWorkflowError(result.error or "module Main exception failed"),
            )
            return self._failed_lane_context(context, exc)
        try:
            acceptance = accept_main_exception_decision(
                self._runner,
                state=context.reporting_state,
                preparation=cast(
                    MainExceptionDecisionPreparation,
                    context.main_preparation,
                ),
                result=result.submission,
                raise_for_terminal_decisions=False,
            )
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            return self._failed_lane_context(context, exc)
        if acceptance.result.decision == "stop_incomplete":
            return context.model_copy(
                deep=True,
                update={
                    "status": "failed",
                    "main_acceptance": acceptance,
                    "error": acceptance.result.rationale,
                },
            )
        return context.model_copy(
            deep=True,
            update={
                "status": (
                    "author_exception_accepted"
                    if acceptance.trigger == "author_response"
                    else "reviewer_exception_accepted"
                ),
                "main_acceptance": acceptance,
            },
        )

    @staticmethod
    async def main_exception_requests_user(
        context: DeclarativeModuleRuntimeLaneContext,
    ) -> bool:
        return bool(
            context.status != "failed"
            and context.main_acceptance is not None
            and context.main_acceptance.result.decision == "request_user"
        )

    async def apply_main_exception_user_input(
        self,
        values: Mapping[str, Any],
    ) -> DeclarativeModuleRuntimeLaneContext:
        context = DeclarativeModuleRuntimeLaneContext.model_validate(values["context"])
        supplied = DeclarativeMainExceptionUserInput.model_validate(values["input"])
        preparation = cast(
            MainExceptionDecisionPreparation,
            context.main_preparation,
        )
        try:
            acceptance = accept_main_exception_decision(
                self._runner,
                state=context.reporting_state,
                preparation=preparation,
                result=WorkflowDecisionSubmission(
                    decision=supplied.decision,
                    rationale=supplied.rationale,
                    finding_ids=list(preparation.exception_ids),
                ),
                raise_for_terminal_decisions=False,
            )
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            return self._failed_lane_context(context, exc)
        if acceptance.result.decision == "stop_incomplete":
            return context.model_copy(
                deep=True,
                update={
                    "status": "failed",
                    "main_acceptance": acceptance,
                    "error": acceptance.result.rationale,
                },
            )
        return context.model_copy(
            deep=True,
            update={
                "status": (
                    "author_exception_accepted"
                    if preparation.trigger == "author_response"
                    else "reviewer_exception_accepted"
                ),
                "main_acceptance": acceptance,
            },
        )

    async def route_after_main_exception(
        self,
        context: DeclarativeModuleRuntimeLaneContext,
    ) -> DeclarativeModuleRuntimeLaneContext:
        if context.status == "failed" or context.main_acceptance is None:
            return context
        if context.main_acceptance.trigger == "author_response":
            return context.model_copy(
                deep=True,
                update={
                    "status": (
                        "revision_pending"
                        if context.main_acceptance.result.decision == "return_to_author"
                        else "recheck_pending"
                    )
                },
            )
        rechecking = cast(DeclarativeModuleRecheckPreparation, context.recheck)
        accepted = rechecking.acceptance
        if accepted is None:
            context.reporting_state["_defer_main_exceptions"] = False
            try:
                accepted = await self._runner._accept_module_recheck(
                    rechecking.prepared,
                    cast(Any, rechecking.submission),
                    context.reporting_state,
                )
            except asyncio.CancelledError:
                raise
            except BaseException as exc:
                return self._failed_lane_context(context, exc)
        return context.model_copy(
            deep=True,
            update={
                "status": (
                    "reviewed" if accepted.next_action == "completed" else "revision_pending"
                ),
                "module": accepted.current,
                "review": cast(
                    DeclarativeModuleReviewPreparation,
                    context.review,
                ).model_copy(update={"acceptance": accepted}),
                "recheck": None,
            },
        )

    def _capture_main_failure(
        self,
        module_id: str,
        exc: BaseException,
    ) -> None:
        self._main_failures[module_id] = exc

    def _capture_review_failure(
        self,
        module_id: str,
        exc: BaseException,
    ) -> None:
        self._review_failures[module_id] = exc

    async def complete_lane(
        self,
        context: DeclarativeModuleRuntimeLaneContext,
    ) -> DeclarativeModuleLaneOutcome:
        if context.status == "reviewed":
            try:
                submission, completion_ref, completion, lane_state = (
                    self._runner._complete_module_lane_attempt(
                        self._restore_attempt(context),
                        cast(Any, context.module),
                    )
                )
            except asyncio.CancelledError:
                raise
            except BaseException as exc:
                context = self._failed_lane_context(context, exc)
            else:
                context = context.model_copy(
                    deep=True,
                    update={
                        "status": "completed",
                        "module": submission,
                        "reporting_state": lane_state,
                        "completion_ref": completion_ref,
                        "completion": completion,
                    },
                )
        status = cast(
            Literal["completed", "deferred", "failed"],
            (
                "deferred"
                if context.status in {"author_exception_deferred", "reviewer_exception_deferred"}
                else context.status
            ),
        )
        include_lane_context = status == "deferred" or (
            status == "failed" and context.resume_status == "preflight_revision_ready"
        )
        include_lane_state = include_lane_context or context.module is not None
        return DeclarativeModuleLaneOutcome(
            module_id=context.module_id,
            status=status,
            module=context.module,
            error=context.error,
            lane_state=(
                {
                    "reporting_state": context.reporting_state,
                    **(
                        {"lane_context": context.model_dump(mode="json")}
                        if include_lane_context
                        else {}
                    ),
                }
                if include_lane_state
                else None
            ),
            completion_ref=context.completion_ref,
            completion=(
                context.completion.model_dump(mode="json")
                if context.completion is not None
                else None
            ),
        )

    def _failed_lane_context(
        self,
        context: DeclarativeModuleRuntimeLaneContext,
        exc: BaseException,
    ) -> DeclarativeModuleRuntimeLaneContext:
        self._runner._fail_module_lane_attempt(
            self._restore_attempt(context),
            exc,
        )
        self._failures[context.module_id] = exc
        if isinstance(exc, DeferredMainDecision):
            status = "deferred"
            reporting_state = getattr(
                exc,
                "lane_state",
                context.reporting_state,
            )
        else:
            status = "failed"
            reporting_state = context.reporting_state
        return context.model_copy(
            deep=True,
            update={
                "status": status,
                "resume_status": context.resume_status or context.status,
                "reporting_state": reporting_state,
                "error": str(exc),
            },
        )

    def _restore_attempt(
        self,
        context: DeclarativeModuleRuntimeLaneContext,
    ) -> _ModuleLaneAttemptContext:
        attempt = cast(DeclarativeModuleLaneAttempt, context.attempt)
        return _ModuleLaneAttemptContext(
            module_id=context.module_id,
            state=context.reporting_state,
            lane_state=context.reporting_state,
            workflow_id=context.workflow_id,
            spec=attempt.spec,
            spec_ref=attempt.spec_ref,
            lane_attempt_id=attempt.lane_attempt_id,
            started_at_ns=attempt.started_at_ns,
            event_store=LocalEventStore(
                self._runner.service.workspace,
                str(context.reporting_state["run_id"]),
            ),
            attempt_ref=attempt.attempt_ref,
        )

    def _restore_authoring(
        self,
        context: DeclarativeModuleRuntimeLaneContext,
    ) -> _ModuleAuthoringPreparationContext:
        authoring = cast(
            DeclarativeModuleAuthoringPreparation,
            context.authoring,
        )
        return _ModuleAuthoringPreparationContext(
            module_id=context.module_id,
            state=context.reporting_state,
            workflow_id=context.workflow_id,
            specialist_id=authoring.specialist_id,
            envelope=authoring.envelope,
            resumed_payload=authoring.resumed_payload,
            revision=authoring.revision,
            review=authoring.review,
            checkpoint=authoring.checkpoint,
        )

    async def reduce_lanes(
        self,
        values: Mapping[str, Any],
    ) -> dict[str, Any]:
        outcomes = {
            module_id: DeclarativeModuleLaneOutcome.model_validate(values[module_id])
            for module_id in REPORT_MODULE_IDS
        }
        results: dict[str, tuple[Any, str, LaneCompletion, dict[str, Any]]] = {}
        for module_id in self._requested_modules:
            outcome = outcomes[module_id]
            if outcome.status == "deferred":
                resumed = (outcome.lane_state or {}).get("reporting_state")
                if isinstance(resumed, dict):
                    resumed["resume"] = True
                try:
                    (
                        submission,
                        completion_ref,
                        completion,
                        lane_state,
                    ) = await self._runner._execute_module_lane(
                        module_id,
                        self.current_state,
                        self._workflow_id,
                        defer_main_exceptions=False,
                        lane_state_override=resumed,
                    )
                except BaseException as exc:
                    self._failures[module_id] = exc
                    continue
                outcome = DeclarativeModuleLaneOutcome(
                    module_id=module_id,
                    status="completed",
                    module=submission,
                    lane_state={"reporting_state": lane_state},
                    completion_ref=completion_ref,
                    completion=completion.model_dump(mode="json"),
                )
            if outcome.status == "failed":
                continue
            lane_state = (outcome.lane_state or {}).get("reporting_state")
            if isinstance(lane_state, dict):
                for key in (
                    "module_submissions",
                    "specialist_submissions",
                    "module_review_completion_refs",
                    "review_exception_refs",
                ):
                    if key in lane_state:
                        if isinstance(lane_state[key], dict):
                            self.current_state.setdefault(key, {}).update(lane_state[key])
                        elif isinstance(lane_state[key], list):
                            current = self.current_state.setdefault(key, [])
                            current.extend(item for item in lane_state[key] if item not in current)
            if (
                outcome.module is not None
                and outcome.completion_ref is not None
                and outcome.completion is not None
                and isinstance(lane_state, dict)
            ):
                results[module_id] = (
                    outcome.module,
                    outcome.completion_ref,
                    LaneCompletion.model_validate(outcome.completion),
                    lane_state,
                )
        failures_by_module = {
            module_id: self._failures.get(module_id)
            or AgentWorkflowError(outcomes[module_id].error or "module lane failed")
            for module_id in self._requested_modules
            if outcomes[module_id].status == "failed" or module_id in self._failures
        }
        failures = [
            failures_by_module[module_id]
            for module_id in self._requested_modules
            if module_id in failures_by_module
        ]
        self._runner._finalize_module_lanes(
            self._requested_modules,
            self.current_state,
            self._workflow_id,
            results,
            failures,
            failures_by_module,
        )
        return self.current_state


class _CurrentTailStages:
    """Call the unchanged stage implementations without subclass recursion."""

    def __init__(self, runner: DeclarativeReportWorkflowRunner) -> None:
        self._runner = runner

    async def _cross_review(self, state: dict, workflow_id: str) -> None:
        await ReportWorkflowRunner._cross_review(self._runner, state, workflow_id)

    async def _chief_edit(self, state: dict, workflow_id: str) -> None:
        await ReportWorkflowRunner._chief_edit(self._runner, state, workflow_id)

    async def _final_review_loop(self, state: dict, workflow_id: str, **values: Any) -> None:
        await ReportWorkflowRunner._final_review_loop(
            self._runner,
            state,
            workflow_id,
            **values,
        )


__all__ = [
    "DeclarativeReportWorkflowRunner",
    "build_reporting_module_stage_definition",
    "execute_declarative_module_stage",
    "resume_declarative_reporting_input",
]

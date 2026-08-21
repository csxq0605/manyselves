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
from manyselves.kernel.contracts import ContractAdapter, build_contract_catalog
from manyselves.kernel.conversations import ConversationRecord
from manyselves.kernel.definitions import (
    AgentDefinition,
    DefinitionKind,
    DefinitionRegistry,
    TaskDefinition,
    WorkflowDefinition,
    specialize_workflow,
)
from manyselves.kernel.executors import (
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
    resume_waiting_input,
)
from manyselves.runtime.state_store import FileWorkflowStateStore
from manyselves.runtime.workflow_host import (
    FileWorkflowEventSink,
    InMemoryWorkflowEventSink,
    WorkflowEventSink,
    WorkflowRuntimeHost,
)

from .agentic_models import TaskEnvelope
from .declarative_cross_owner_cohort import (
    DeclarativeCrossOwnerRuntime,
    compile_cross_owner_workflows,
    register_cross_owner_pipeline_specializations,
    retry_failed_cross_owner_pipelines,
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
from .distributed_runtime import LocalEventStore
from .models import REPORT_MODULE_IDS
from .parallel_runtime import LaneCompletion
from .review_lifecycle import DeferredMainDecision
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

    async def prepare_revision_lane(
        self,
        context: DeclarativeModuleRuntimeLaneContext,
    ) -> DeclarativeModuleRuntimeLaneContext: ...

    async def accept_revision_lane(
        self,
        values: Mapping[str, Any],
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

    async def continue_review_lane(
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
        invoker = self._task_routes.get(task.id, self._default)
        return await invoker.invoke(
            agent,
            task,
            value,
            conversation,
            task_id=task_id,
        )


def _reporting_agent_invokers(
    module_invokers: Mapping[str, AgentInvoker],
    cross_invokers: Mapping[str, AgentInvoker],
) -> dict[str, AgentInvoker]:
    """Compose module and Cross adapters for their shared Author identities."""

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
    return combined


def build_reporting_module_stage_definition() -> tuple[DefinitionRegistry, WorkflowDefinition]:
    """Compatibility loader for the packaged top-level Reporting workflow."""

    _capability, registry = load_distribution_reporting_capability()
    register_cross_owner_pipeline_specializations(registry)
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
    subworkflows: dict[str, ResolvedPlan]


def _compile_reporting_runtime(
    reporting_state: Mapping[str, Any],
    *,
    full_report: bool,
) -> _CompiledReportingRuntime:
    """Compile the top-level plan and every file-defined nested workflow."""

    definitions, workflow = build_reporting_module_stage_definition()
    runtime_lanes = register_module_runtime_lane_specializations(definitions)
    workflow.state = {
        "reporting-state": deepcopy(dict(reporting_state)),
        "full-report": full_report,
    }
    executors = build_builtin_executor_registry()
    compiler = WorkflowCompiler(executors)
    plan = compiler.compile(workflow, definitions)

    tail = definitions.require(
        DefinitionKind.WORKFLOW,
        "distribution-reporting-tail",
    )
    if not isinstance(tail, WorkflowDefinition):
        raise TypeError("distribution-reporting-tail is not a workflow")
    tail_plan = compiler.compile(tail, definitions)
    cross_cohort_plan, cross_pipeline_plans = compile_cross_owner_workflows(
        definitions,
        executors,
    )
    cohort = definitions.require(
        DefinitionKind.WORKFLOW,
        "distribution-module-cohort",
    )
    if not isinstance(cohort, WorkflowDefinition):
        raise TypeError("distribution-module-cohort is not a workflow")
    cohort = specialize_workflow(
        cohort,
        {"max_concurrency": len(REPORT_MODULE_IDS)},
    )
    cohort_plan = compiler.compile(cohort, definitions)
    runtime_lane_plans = {
        workflow_id: compiler.compile(runtime_lane, definitions)
        for workflow_id, runtime_lane in runtime_lanes.items()
    }
    return _CompiledReportingRuntime(
        definitions=definitions,
        contracts=build_contract_catalog(definitions),
        executors=executors,
        plan=plan,
        cohort_plan=cohort_plan,
        cross_cohort_plan=cross_cohort_plan,
        subworkflows={
            cohort.id: cohort_plan,
            **runtime_lane_plans,
            tail.id: tail_plan,
            "distribution-cross-owner-cohort": cross_cohort_plan,
            **cross_pipeline_plans,
        },
    )


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
    reporting_state = state.variables.get("reporting-state", {})
    compiled = _compile_reporting_runtime(
        reporting_state if isinstance(reporting_state, Mapping) else {},
        full_report=bool(state.variables.get("full-report", False)),
    )
    resumed = resume_waiting_input(
        state_store.load_plan(run_id),
        state,
        input_id=input_id,
        values=values,
        contracts=compiled.contracts,
        subworkflows=compiled.subworkflows,
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
) -> WorkflowState:
    """Run current modules and the file-defined tail in one parent state."""

    full_report = set(requested_modules) == set(REPORT_MODULE_IDS)
    compiled = _compile_reporting_runtime(state, full_report=full_report)
    definitions = compiled.definitions
    executors = compiled.executors
    plan = compiled.plan
    cohort_plan = compiled.cohort_plan
    cross_cohort_plan = compiled.cross_cohort_plan
    if module_runtime is None:
        if execute_current is None:
            raise TypeError("module runtime is required")
        module_runtime = _BatchModuleRuntime(
            execute_current,
            requested_modules,
            state,
            workflow_id,
        )
    kernel_run_id = str(state["run_id"])
    try:
        kernel_state = state_store.load(kernel_run_id)
        kernel_state.variables["reporting-state"] = deepcopy(state)
        kernel_state.variables["full-report"] = full_report
        saved_tail = kernel_state.subworkflow_states.get("run-reporting-tail")
        if saved_tail is not None:
            child = WorkflowState.model_validate(saved_tail)
            child.variables["reporting-state"] = deepcopy(state)
            saved_cross = child.subworkflow_states.get("run-cross")
            if saved_cross is not None:
                cross_state = WorkflowState.model_validate(saved_cross)
                cross_state.variables["reporting-state"] = deepcopy(state)
                cross_state.variables["prepared-cross-state"] = deepcopy(state)
                cross_state = retry_failed_cross_owner_pipelines(
                    cross_cohort_plan,
                    cross_state,
                )
                child.subworkflow_states["run-cross"] = cross_state.model_dump(mode="json")
            kernel_state.subworkflow_states["run-reporting-tail"] = child.model_dump(mode="json")
        saved_cohort = kernel_state.subworkflow_states.get("run-module-cohort")
        if saved_cohort is not None:
            child = WorkflowState.model_validate(saved_cohort)
            child.variables["module-inputs"] = deepcopy(state)
            child.variables["prepared-module-inputs"] = deepcopy(state)
            child = _retry_failed_module_lanes(
                cohort_plan,
                child,
                tuple(REPORT_MODULE_IDS),
            )
            kernel_state.subworkflow_states["run-module-cohort"] = child.model_dump(mode="json")
    except FileNotFoundError:
        kernel_state = WorkflowState.for_plan(kernel_run_id, plan)
        save_plan = getattr(state_store, "save_plan", None)
        if callable(save_plan):
            save_plan(kernel_run_id, plan)

    tail_adapters = _ReportingTailAdapters(tail_runner, workflow_id)
    cross_runtime = DeclarativeCrossOwnerRuntime(
        tail_runner,
        state,
        workflow_id,
        compatibility_cross=tail_adapters.cross,
    )
    module_tools = {
        "start-current-module-lane": lambda values: module_runtime.start_lane(
            str(values["module_id"]),
            values["state"],
            workflow_id,
        ),
        "prepare-current-module-authoring": module_runtime.prepare_author_lane,
        "module-authoring-requires-agent": module_runtime.author_requires_agent,
        "accept-current-module-authoring": module_runtime.accept_author_lane,
        "resume-current-module-authoring": module_runtime.resume_author_lane,
        "module-lane-can-review": module_runtime.can_review_lane,
        "prepare-current-module-review": module_runtime.prepare_review_lane,
        "module-review-requires-agent": module_runtime.review_requires_agent,
        "accept-current-module-review": module_runtime.accept_review_lane,
        "module-review-needs-revision": module_runtime.review_needs_revision,
        "prepare-current-module-revision": module_runtime.prepare_revision_lane,
        "accept-current-module-revision": module_runtime.accept_revision_lane,
        "prepare-current-module-recheck": module_runtime.prepare_recheck_lane,
        "module-recheck-requires-agent": module_runtime.recheck_requires_agent,
        "accept-current-module-recheck": module_runtime.accept_recheck_lane,
        "continue-current-module-review": module_runtime.continue_review_lane,
        "complete-current-module-lane": module_runtime.complete_lane,
    }
    module_tools["prepare-module-cohort"] = module_runtime.prepare_lanes
    module_tools["reduce-module-cohort"] = module_runtime.reduce_lanes
    try:
        completed = await WorkflowRuntimeHost(
            executors,
            state_store,
            event_sink if event_sink is not None else InMemoryWorkflowEventSink(),
        ).execute(
            plan,
            kernel_state,
            RuntimeContext(
                tools={
                    **module_tools,
                    "prepare-cross-owner-cohort": cross_runtime.prepare,
                    "prepare-current-cross-owner-initial": cross_runtime.prepare_initial,
                    "cross-owner-initial-requires-agent": (
                        cross_runtime.initial_requires_agent
                    ),
                    "accept-current-cross-owner-initial": cross_runtime.accept_initial,
                    "cross-owner-initial-has-findings": (
                        cross_runtime.initial_has_findings
                    ),
                    "prepare-current-cross-owner-revision": (
                        cross_runtime.prepare_revision
                    ),
                    "cross-owner-revision-requires-agent": (
                        cross_runtime.revision_requires_agent
                    ),
                    "accept-current-cross-owner-revision": (
                        cross_runtime.accept_revision
                    ),
                    "prepare-current-cross-owner-author-exception": (
                        cross_runtime.prepare_author_exception
                    ),
                    "prepare-current-cross-owner-reviewer-exception": (
                        cross_runtime.prepare_reviewer_exception
                    ),
                    "cross-owner-main-exception-requires-agent": (
                        cross_runtime.main_exception_requires_agent
                    ),
                    "accept-current-cross-owner-main-exception": (
                        cross_runtime.accept_main_exception
                    ),
                    "cross-owner-main-exception-requests-user": (
                        cross_runtime.main_exception_requests_user
                    ),
                    "apply-current-cross-owner-main-exception-user-input": (
                        cross_runtime.apply_main_exception_user_input
                    ),
                    "cross-owner-author-exception-returns-to-author": (
                        cross_runtime.author_exception_returns_to_author
                    ),
                    "prepare-current-cross-owner-local-review": (
                        cross_runtime.prepare_local_review
                    ),
                    "cross-owner-local-review-requires-agent": (
                        cross_runtime.local_review_requires_agent
                    ),
                    "accept-current-cross-owner-local-review": (
                        cross_runtime.accept_local_review
                    ),
                    "prepare-current-cross-owner-recheck": (
                        cross_runtime.prepare_recheck
                    ),
                    "cross-owner-recheck-requires-agent": (
                        cross_runtime.recheck_requires_agent
                    ),
                    "accept-current-cross-owner-recheck": (
                        cross_runtime.accept_recheck
                    ),
                    "advance-current-cross-owner-round": cross_runtime.advance_round,
                    "cross-owner-round-needs-revision": (
                        cross_runtime.round_needs_revision
                    ),
                    "complete-current-cross-owner-pipeline": (
                        cross_runtime.complete_owner_round
                    ),
                    "complete-current-cross-owner-without-findings": (
                        cross_runtime.complete_owner_without_findings
                    ),
                    "reduce-cross-owner-cohort": cross_runtime.reduce,
                    "run-reporting-chief": tail_adapters.chief,
                    "run-reporting-final": tail_adapters.final,
                    "run-reporting-delivery": tail_adapters.delivery,
                },
                agents=_reporting_agent_invokers(
                    module_runtime.agent_invokers,
                    cross_runtime.agent_invokers,
                ),
                contracts=compiled.contracts,
                definitions=definitions,
                subworkflows=compiled.subworkflows,
            ),
        )
    except BaseException:
        current = (
            tail_adapters.current_state
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
            tail_adapters.current_state
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
    ) -> DeclarativeModuleRuntimeLaneContext:
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

    async def continue_review_lane(
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

    async def _run_module_lanes(
        self,
        requested_modules: tuple[str, ...],
        state: dict,
        workflow_id: str,
    ) -> None:
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
        _agent: AgentDefinition,
        _task: TaskDefinition,
        value: Any,
        conversation: ConversationRecord,
        *,
        task_id: str,
    ) -> AgentInvocationOutcome:
        del task_id
        context = DeclarativeModuleRuntimeLaneContext.model_validate(value)
        if context.revision is not None:
            preparation = context.revision.prepared
            try:
                payload = await self._runner._agent(
                    preparation.specialist_id,
                    preparation.envelope,
                    preparation.envelope.input_refs,
                    context.workflow_id,
                    session_key=conversation.key.value,
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
        try:
            payload = await self._runner._agent(
                authoring.specialist_id,
                authoring.envelope,
                authoring.envelope.input_refs,
                context.workflow_id,
                session_key=conversation.key.value,
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
        _agent: AgentDefinition,
        _task: TaskDefinition,
        value: Any,
        conversation: ConversationRecord,
        *,
        task_id: str,
    ) -> AgentInvocationOutcome:
        del task_id
        context = DeclarativeModuleRuntimeLaneContext.model_validate(value)
        if context.recheck is not None:
            preparation = context.recheck.prepared
            envelope = cast(TaskEnvelope, preparation.envelope)
            try:
                payload = await self._runner._agent(
                    "evidence-auditor",
                    envelope,
                    envelope.input_refs,
                    context.workflow_id,
                    session_key=conversation.key.value,
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
        envelope = cast(TaskEnvelope, reviewing.prepared.envelope)
        try:
            payload = await self._runner._agent(
                "evidence-auditor",
                envelope,
                envelope.input_refs,
                context.workflow_id,
                session_key=conversation.key.value,
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
        author_invoker = _CurrentModuleAuthorInvoker(
            runner,
            self._capture_author_failure,
        )
        reviewer_invoker = _CurrentModuleReviewerInvoker(
            runner,
            self._capture_review_failure,
        )
        self.agent_invokers: Mapping[str, AgentInvoker] = {
            **{f"module-{module_id}-specialist": author_invoker for module_id in REPORT_MODULE_IDS},
            "evidence-auditor": reviewer_invoker,
        }

    async def prepare_lanes(self, state: dict[str, Any]) -> dict[str, Any]:
        return state

    async def start_lane(
        self,
        module_id: str,
        lane_state: dict[str, Any],
        workflow_id: str,
    ) -> DeclarativeModuleRuntimeLaneContext:
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
        try:
            preparation = await self._runner._prepare_module_initial_review(
                context.module_id,
                cast(Any, context.module),
                context.reporting_state,
                context.workflow_id,
                initial_scope=set(REPORT_TAXONOMY[context.module_id].submodules),
            )
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            return self._failed_lane_context(context, exc)
        return context.model_copy(
            deep=True,
            update={
                "status": (
                    "review_ready" if preparation.mode == "invoke_agent" else "review_resumed"
                ),
                "module": preparation.current,
                "review": DeclarativeModuleReviewPreparation(
                    envelope=cast(Any, preparation.envelope),
                    reviewer_session_key=preparation.reviewer_session_key,
                    prepared=preparation,
                ),
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

    async def continue_review_lane(
        self,
        context: DeclarativeModuleRuntimeLaneContext,
    ) -> DeclarativeModuleRuntimeLaneContext:
        if context.status == "reviewed":
            return context
        if context.status != "review_resumed":
            return context
        context.reporting_state["resume"] = True
        try:
            reviewed = await self._runner._module_review_loop(
                context.module_id,
                cast(Any, context.module),
                context.reporting_state,
                context.workflow_id,
                initial_scope=set(REPORT_TAXONOMY[context.module_id].submodules),
            )
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            return self._failed_lane_context(context, exc)
        return context.model_copy(
            deep=True,
            update={
                "status": "reviewed",
                "module": reviewed,
                "review": None,
                "recheck": None,
            },
        )

    async def review_needs_revision(
        self,
        context: DeclarativeModuleRuntimeLaneContext,
    ) -> bool:
        return context.status == "revision_pending"

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
        try:
            preparation = await self._runner._prepare_module_recheck(
                context.module_id,
                cast(Any, context.module),
                context.reporting_state,
                context.workflow_id,
                initial_scope=set(REPORT_TAXONOMY[context.module_id].submodules),
                lifecycle_id="initial",
            )
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            return self._failed_lane_context(context, exc)
        return context.model_copy(
            deep=True,
            update={
                "status": (
                    "recheck_ready" if preparation.mode == "invoke_agent" else "review_resumed"
                ),
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
        try:
            accepted = await self._runner._accept_module_recheck(
                cast(DeclarativeModuleRecheckPreparation, context.recheck).prepared,
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
                "recheck": None,
            },
        )

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
            context.status,
        )
        include_lane_state = status == "deferred" or context.module is not None
        return DeclarativeModuleLaneOutcome(
            module_id=context.module_id,
            status=status,
            module=context.module,
            error=context.error,
            lane_state=(
                {"reporting_state": context.reporting_state} if include_lane_state else None
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

    def _deliver(self, state: dict) -> None:
        ReportWorkflowRunner._deliver(self._runner, state)


__all__ = [
    "DeclarativeReportWorkflowRunner",
    "build_reporting_module_stage_definition",
    "execute_declarative_module_stage",
    "resume_declarative_reporting_input",
]

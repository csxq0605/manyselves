"""Declarative Reporting tail bound to the current stage implementations."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from inspect import isawaitable
from typing import Any

from manyselves.capabilities.distribution_reporting import (
    load_distribution_reporting_capability,
)
from manyselves.capabilities.distribution_reporting.runtime.models.agentic import ModuleSubmission
from manyselves.kernel.contracts import ContractAdapter, build_contract_catalog
from manyselves.kernel.definitions import (
    DefinitionKind,
    DefinitionRegistry,
    WorkflowDefinition,
)
from manyselves.kernel.executors import RuntimeContext, build_builtin_executor_registry
from manyselves.kernel.ports import WorkflowStateStore
from manyselves.kernel.workflow import WorkflowCompiler, WorkflowState, WorkflowStatus
from manyselves.runtime.semantic_trace import SemanticEventKind, SemanticTraceRecorder
from manyselves.runtime.workflow_host import (
    InMemoryWorkflowEventSink,
    WorkflowRuntimeHost,
)

from .declarative_chief_chapter_cohort import (
    DeclarativeChiefChapterRuntime,
    compile_chief_chapter_workflows,
    register_chief_chapter_lane_specializations,
    retry_failed_chief_chapter_lanes,
)
from .declarative_cross_owner_cohort import (
    DeclarativeCrossOwnerRuntime,
    compile_cross_owner_workflows,
    register_cross_owner_pipeline_specializations,
    retry_failed_cross_owner_pipelines,
)
from .declarative_delivery import (
    DeclarativeDeliveryRuntime,
    compile_delivery_workflow,
)
from .declarative_final_chapter_cohort import (
    DeclarativeFinalChapterRuntime,
    compile_final_chapter_workflows,
    register_final_chapter_lane_specializations,
    retry_failed_final_chapter_lanes,
)
from .declarative_final_review_cycle import (
    DeclarativeFinalReviewRuntime,
    compile_final_review_workflows,
    compose_final_review_agent_invokers,
    register_final_review_lane_specializations,
)


class DeclarativeReportingTailError(RuntimeError):
    """Raised when the Reporting tail does not reach delivery completion."""


def build_reporting_tail_definition() -> tuple[
    DefinitionRegistry, dict[str, ContractAdapter], WorkflowDefinition
]:
    """Load the Reporting-owned Cross through Delivery workflow file."""

    _capability, registry = load_distribution_reporting_capability()
    register_cross_owner_pipeline_specializations(registry)
    register_chief_chapter_lane_specializations(registry)
    register_final_chapter_lane_specializations(registry)
    register_final_review_lane_specializations(registry)
    workflow = registry.require(
        DefinitionKind.WORKFLOW,
        "distribution-reporting-tail",
    )
    if not isinstance(workflow, WorkflowDefinition):
        raise TypeError("distribution-reporting-tail is not a workflow")
    return registry, build_contract_catalog(registry), workflow.model_copy(deep=True)


async def execute_declarative_reporting_tail(
    *,
    runner: Any,
    state: dict[str, Any],
    workflow_id: str,
    state_store: WorkflowStateStore,
    trace: SemanticTraceRecorder | None = None,
    publish_tool: Any | None = None,
) -> WorkflowState:
    """Run current tail stages through neutral actions without changing default routing."""

    definitions, contracts, workflow = build_reporting_tail_definition()
    workflow.state = {"reporting-state": deepcopy(state)}
    executors = build_builtin_executor_registry()
    plan = WorkflowCompiler(executors).compile(workflow, definitions)
    cross_cohort_plan, cross_pipeline_plans = compile_cross_owner_workflows(
        definitions,
        executors,
    )
    chief_cohort_plan, chief_lane_plans = compile_chief_chapter_workflows(
        definitions,
        executors,
    )
    final_cohort_plan, final_lane_plans = compile_final_chapter_workflows(
        definitions,
        executors,
    )
    final_review_plans = compile_final_review_workflows(definitions, executors)
    delivery_plan = compile_delivery_workflow(definitions, executors)
    kernel_run_id = str(state["run_id"])
    try:
        kernel_state = state_store.load(kernel_run_id)
        kernel_state.variables["reporting-state"] = deepcopy(state)
        saved_cross = kernel_state.subworkflow_states.get("run-cross")
        if saved_cross is not None:
            cross_state = WorkflowState.model_validate(saved_cross)
            cross_state.variables["reporting-state"] = deepcopy(state)
            cross_state.variables["prepared-cross-state"] = deepcopy(state)
            cross_state = retry_failed_cross_owner_pipelines(
                cross_cohort_plan,
                cross_state,
            )
            kernel_state.subworkflow_states["run-cross"] = cross_state.model_dump(mode="json")
        saved_chief = kernel_state.subworkflow_states.get("run-chief")
        if saved_chief is not None:
            chief_state = WorkflowState.model_validate(saved_chief)
            chief_state.variables["reporting-state"] = deepcopy(state)
            chief_state.variables["prepared-chief-state"] = deepcopy(state)
            chief_state = retry_failed_chief_chapter_lanes(
                chief_cohort_plan,
                chief_state,
            )
            kernel_state.subworkflow_states["run-chief"] = chief_state.model_dump(mode="json")
        saved_final = kernel_state.subworkflow_states.get("run-final")
        if saved_final is not None:
            final_state = WorkflowState.model_validate(saved_final)
            final_state.variables["reporting-state"] = deepcopy(state)
            final_state.variables["prepared-final-state"] = deepcopy(state)
            final_state = retry_failed_final_chapter_lanes(
                final_cohort_plan,
                final_state,
                subworkflows=final_review_plans,
            )
            kernel_state.subworkflow_states["run-final"] = final_state.model_dump(mode="json")
    except FileNotFoundError:
        kernel_state = WorkflowState.for_plan(kernel_run_id, plan)
        save_plan = getattr(state_store, "save_plan", None)
        if callable(save_plan):
            save_plan(kernel_run_id, plan)
    adapters = _ReportingTailAdapters(runner, workflow_id)
    stage_tools = {
        "cross": adapters.cross,
        "chief": adapters.chief,
    }
    if trace is not None:
        trace.record(
            SemanticEventKind.WORKFLOW_STARTED,
            workflow_id=workflow_id,
            status="running",
        )
        stage_tools = {
            stage: _traced_stage(
                stage,
                invoke,
                trace=trace,
                workflow_id=workflow_id,
            )
            for stage, invoke in stage_tools.items()
        }
    cross_runtime = DeclarativeCrossOwnerRuntime(
        runner,
        state,
        workflow_id,
        compatibility_cross=stage_tools["cross"],
    )
    chief_runtime = DeclarativeChiefChapterRuntime(
        runner,
        state,
        workflow_id,
        compatibility_chief=stage_tools["chief"],
    )
    final_runtime = DeclarativeFinalChapterRuntime(
        runner,
        state,
        workflow_id,
    )
    final_review_runtime = DeclarativeFinalReviewRuntime(runner, state, workflow_id)
    delivery_runtime = DeclarativeDeliveryRuntime(
        runner,
        publish_tool=publish_tool,
    )
    delivery_tools = {
        "prepare-render-delivery": delivery_runtime.prepare,
        "publish-materialize-delivery": delivery_runtime.publish,
        "complete-delivery": delivery_runtime.complete,
    }
    if trace is not None:
        delivery_tools = {
            tool_id: _traced_action(
                tool_id,
                tool_id,
                invoke,
                trace=trace,
                workflow_id=workflow_id,
            )
            for tool_id, invoke in delivery_tools.items()
        }
    final_agents = compose_final_review_agent_invokers(
        {
            **cross_runtime.agent_invokers,
            **chief_runtime.agent_invokers,
            **final_runtime.agent_invokers,
        },
        final_review_runtime,
    )
    try:
        completed = await WorkflowRuntimeHost(
            executors,
            state_store,
            InMemoryWorkflowEventSink(),
        ).execute(
            plan,
            kernel_state,
            RuntimeContext(
                tools={
                    **delivery_tools,
                    "prepare-chief-chapter-cohort": chief_runtime.prepare,
                    "prepare-current-chief-chapter": chief_runtime.prepare_lane,
                    "chief-chapter-requires-agent": chief_runtime.requires_agent,
                    "accept-current-chief-chapter": chief_runtime.accept_lane,
                    "complete-current-chief-chapter": chief_runtime.complete_lane,
                    "reduce-chief-chapter-cohort": chief_runtime.reduce,
                    "prepare-final-chapter-cohort": final_runtime.prepare,
                    "prepare-current-final-chapter": final_runtime.prepare_lane,
                    "final-chapter-initial-requires-agent": (final_runtime.requires_agent),
                    "accept-current-final-chapter-initial": final_runtime.accept_lane,
                    "complete-current-final-chapter": final_runtime.complete_lane,
                    "reduce-final-chapter-cohort": final_runtime.reduce,
                    "start-final-review-cycle": final_review_runtime.start_cycle,
                    "final-review-needs-round": final_review_runtime.needs_round,
                    "advance-final-review-round": final_review_runtime.advance_round,
                    "prepare-current-final-chief-revision": (
                        final_review_runtime.prepare_chief_revision
                    ),
                    "final-chief-revision-requires-agent": (
                        final_review_runtime.chief_revision_requires_agent
                    ),
                    "accept-current-final-chief-revision": (
                        final_review_runtime.accept_chief_revision
                    ),
                    "complete-current-final-chief-revision": (
                        final_review_runtime.complete_chief_revision
                    ),
                    "reduce-final-chief-revision-cohort": (
                        final_review_runtime.reduce_chief_revisions
                    ),
                    "prepare-current-final-recheck": (final_review_runtime.prepare_recheck),
                    "final-recheck-requires-agent": (final_review_runtime.recheck_requires_agent),
                    "accept-current-final-recheck": (final_review_runtime.accept_recheck),
                    "complete-current-final-recheck": (final_review_runtime.complete_recheck),
                    "reduce-final-recheck-cohort": (final_review_runtime.reduce_rechecks),
                    "complete-final-review": final_review_runtime.complete_review,
                    "prepare-cross-owner-cohort": cross_runtime.prepare,
                    "prepare-current-cross-owner-initial": cross_runtime.prepare_initial,
                    "cross-owner-initial-requires-agent": (cross_runtime.initial_requires_agent),
                    "accept-current-cross-owner-initial": cross_runtime.accept_initial,
                    "cross-owner-initial-has-findings": (cross_runtime.initial_has_findings),
                    "prepare-current-cross-owner-revision": (cross_runtime.prepare_revision),
                    "cross-owner-revision-requires-agent": (cross_runtime.revision_requires_agent),
                    "accept-current-cross-owner-revision": (cross_runtime.accept_revision),
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
                },
                agents=final_agents,
                contracts=contracts,
                definitions=definitions,
                subworkflows={
                    "distribution-cross-owner-cohort": cross_cohort_plan,
                    **cross_pipeline_plans,
                    "distribution-chief-chapter-cohort": chief_cohort_plan,
                    **chief_lane_plans,
                    "distribution-final-chapter-cohort": final_cohort_plan,
                    **final_lane_plans,
                    **final_review_plans,
                    "distribution-report-delivery": delivery_plan,
                },
            ),
        )
    except BaseException:
        _replace_state(
            state,
            delivery_runtime.current_state
            or adapters.current_state
            or final_review_runtime.current_state
            or final_runtime.current_state
            or chief_runtime.current_state
            or cross_runtime.current_state,
        )
        raise
    _replace_state(state, completed.outputs["result"])
    if completed.status is not WorkflowStatus.COMPLETED or "delivery_completion_ref" not in state:
        raise DeclarativeReportingTailError("declarative Reporting tail did not deliver")
    if trace is not None:
        trace.record(
            SemanticEventKind.OUTPUT_PUBLISHED,
            workflow_id=workflow_id,
            action_id="finish-reporting-tail",
            output_contract="reporting_tail_state",
            output_id=str(state["delivery_completion_ref"]),
            status="completed",
        )
        trace.record(
            SemanticEventKind.WORKFLOW_COMPLETED,
            workflow_id=workflow_id,
            status="completed",
        )
    return completed


class _ReportingTailAdapters:
    def __init__(self, runner: Any, workflow_id: str) -> None:
        self._runner = runner
        self._workflow_id = workflow_id
        self.current_state: dict[str, Any] = {}

    async def cross(self, state: dict[str, Any]) -> dict[str, Any]:
        _restore_module_submissions(state)
        self.current_state = state
        if "cross_review_completion_ref" not in state:
            await self._runner._cross_review(state, self._workflow_id)
        return state

    async def chief(self, state: dict[str, Any]) -> dict[str, Any]:
        _restore_module_submissions(state)
        self.current_state = state
        if "final_review_completion_ref" not in state and "chief_candidate_ref" not in state:
            await self._runner._chief_edit(state, self._workflow_id)
        return state


def _replace_state(target: dict[str, Any], value: Mapping[str, Any]) -> None:
    restored = dict(value)
    target.clear()
    target.update(restored)


def _restore_module_submissions(state: dict[str, Any]) -> None:
    modules = state.get("module_submissions")
    if not isinstance(modules, Mapping):
        return
    state["module_submissions"] = {
        module_id: ModuleSubmission.model_validate(value) for module_id, value in modules.items()
    }


def _traced_stage(
    stage: str,
    invoke: Any,
    *,
    trace: SemanticTraceRecorder,
    workflow_id: str,
):
    return _traced_action(
        f"run-{stage}",
        f"run-reporting-{stage}",
        invoke,
        trace=trace,
        workflow_id=workflow_id,
    )


def _traced_action(
    action_id: str,
    tool_id: str,
    invoke: Any,
    *,
    trace: SemanticTraceRecorder,
    workflow_id: str,
):
    async def traced(state: dict[str, Any]) -> dict[str, Any]:
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
        try:
            result = invoke(state)
            if isawaitable(result):
                result = await result
        except BaseException:
            trace.record(
                SemanticEventKind.ACTION_FAILED,
                workflow_id=workflow_id,
                action_id=action_id,
                status="failed",
            )
            raise
        trace.record(
            SemanticEventKind.ACTION_COMPLETED,
            workflow_id=workflow_id,
            action_id=action_id,
            status="completed",
        )
        return result

    return traced

"""Declarative Reporting tail bound to the current stage implementations."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from inspect import isawaitable
from typing import Any

from manyselves.capabilities.distribution_reporting import (
    load_distribution_reporting_capability,
)
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

from .agentic_models import ModuleSubmission
from .declarative_cross_owner_cohort import (
    DeclarativeCrossOwnerRuntime,
    compile_cross_owner_workflows,
    register_cross_owner_pipeline_specializations,
    retry_failed_cross_owner_pipelines,
)
from .models import REPORT_MODULE_IDS


class DeclarativeReportingTailError(RuntimeError):
    """Raised when the Reporting tail does not reach delivery completion."""


def build_reporting_tail_definition() -> tuple[
    DefinitionRegistry, dict[str, ContractAdapter], WorkflowDefinition
]:
    """Load the Reporting-owned Cross through Delivery workflow file."""

    _capability, registry = load_distribution_reporting_capability()
    register_cross_owner_pipeline_specializations(registry)
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
    except FileNotFoundError:
        kernel_state = WorkflowState.for_plan(kernel_run_id, plan)
        save_plan = getattr(state_store, "save_plan", None)
        if callable(save_plan):
            save_plan(kernel_run_id, plan)
    adapters = _ReportingTailAdapters(runner, workflow_id)
    stage_tools = {
        "cross": adapters.cross,
        "chief": adapters.chief,
        "final": adapters.final,
        "delivery": adapters.delivery,
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
                    **{
                        f"run-reporting-{stage}": invoke
                        for stage, invoke in stage_tools.items()
                        if stage != "cross"
                    },
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
                    "continue-current-cross-owner-pipeline": cross_runtime.continue_owner,
                    "reduce-cross-owner-cohort": cross_runtime.reduce,
                },
                agents=cross_runtime.agent_invokers,
                contracts=contracts,
                definitions=definitions,
                subworkflows={
                    "distribution-cross-owner-cohort": cross_cohort_plan,
                    **cross_pipeline_plans,
                },
            ),
        )
    except BaseException:
        _replace_state(
            state,
            cross_runtime.current_state or adapters.current_state,
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

    async def final(self, state: dict[str, Any]) -> dict[str, Any]:
        _restore_module_submissions(state)
        self.current_state = state
        if "final_review_completion_ref" in state:
            return state
        claims = [
            claim
            for module_id in REPORT_MODULE_IDS
            for claim in state["module_submissions"][module_id].claims
        ]
        await self._runner._final_review_loop(
            state,
            self._workflow_id,
            chief_envelope=state.get("chief_editor_envelope"),
            chief_session_key=state["chief_editor_session_key"],
            approved_module_text=state["approved_module_text"],
            claims=claims,
        )
        return state

    def delivery(self, state: dict[str, Any]) -> dict[str, Any]:
        _restore_module_submissions(state)
        self.current_state = state
        if "delivery_completion_ref" not in state:
            self._runner._deliver(state)
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
    async def traced(state: dict[str, Any]) -> dict[str, Any]:
        action_id = f"run-{stage}"
        trace.record(
            SemanticEventKind.ACTION_STARTED,
            workflow_id=workflow_id,
            action_id=action_id,
        )
        trace.record(
            SemanticEventKind.TOOL_INVOKED,
            workflow_id=workflow_id,
            action_id=action_id,
            tool_id=f"run-reporting-{stage}",
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

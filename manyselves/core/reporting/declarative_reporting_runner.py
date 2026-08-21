"""Explicit declarative path over the complete current Reporting lifecycle."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from copy import deepcopy
from typing import Any, Protocol

from manyselves.capabilities.distribution_reporting import (
    load_distribution_reporting_capability,
)
from manyselves.kernel.contracts import build_contract_catalog
from manyselves.kernel.definitions import (
    DefinitionKind,
    DefinitionRegistry,
    WorkflowDefinition,
    specialize_workflow,
)
from manyselves.kernel.executors import RuntimeContext, build_builtin_executor_registry
from manyselves.kernel.ports import WorkflowStateStore
from manyselves.kernel.workflow import WorkflowCompiler, WorkflowState
from manyselves.runtime.state_store import FileWorkflowStateStore
from manyselves.runtime.workflow_host import (
    FileWorkflowEventSink,
    InMemoryWorkflowEventSink,
    WorkflowEventSink,
    WorkflowRuntimeHost,
)

from .declarative_module_cohort import (
    DeclarativeModuleLaneOutcome,
    _retry_failed_module_lanes,
)
from .declarative_reporting_tail import _ReportingTailAdapters
from .models import REPORT_MODULE_IDS
from .parallel_runtime import LaneCompletion
from .review_lifecycle import DeferredMainDecision
from .workflow import AgentWorkflowError, ReportWorkflowRunner


class ReportingModuleRuntime(Protocol):
    """Capability adapter bound to the file-defined module Cohort actions."""

    current_state: dict[str, Any] | None

    async def prepare_lanes(self, state: dict[str, Any]) -> dict[str, Any]: ...

    async def execute_lane(
        self,
        module_id: str,
        state: dict[str, Any],
        workflow_id: str,
    ) -> Any: ...

    async def reduce_lanes(self, outcomes: Mapping[str, Any]) -> dict[str, Any]: ...


def build_reporting_module_stage_definition(
) -> tuple[DefinitionRegistry, WorkflowDefinition]:
    """Compatibility loader for the packaged top-level Reporting workflow."""

    _capability, registry = load_distribution_reporting_capability()
    workflow = registry.require(
        DefinitionKind.WORKFLOW,
        "distribution-reporting",
    )
    if not isinstance(workflow, WorkflowDefinition):
        raise TypeError("distribution-reporting is not a workflow")
    return registry, workflow.model_copy(deep=True)


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

    definitions, workflow = build_reporting_module_stage_definition()
    full_report = set(requested_modules) == set(REPORT_MODULE_IDS)
    workflow.state = {
        "reporting-state": deepcopy(state),
        "full-report": full_report,
    }
    executors = build_builtin_executor_registry()
    plan = WorkflowCompiler(executors).compile(workflow, definitions)
    tail = definitions.require(
        DefinitionKind.WORKFLOW,
        "distribution-reporting-tail",
    )
    if not isinstance(tail, WorkflowDefinition):
        raise TypeError("distribution-reporting-tail is not a workflow")
    tail_plan = WorkflowCompiler(executors).compile(tail, definitions)
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
    cohort_plan = WorkflowCompiler(executors).compile(cohort, definitions)
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
            kernel_state.subworkflow_states["run-reporting-tail"] = child.model_dump(
                mode="json"
            )
        saved_cohort = kernel_state.subworkflow_states.get("run-module-cohort")
        if saved_cohort is not None:
            child = WorkflowState.model_validate(saved_cohort)
            child.variables["module-inputs"] = deepcopy(state)
            child = _retry_failed_module_lanes(
                cohort_plan,
                child,
                tuple(REPORT_MODULE_IDS),
            )
            kernel_state.subworkflow_states["run-module-cohort"] = child.model_dump(
                mode="json"
            )
    except FileNotFoundError:
        kernel_state = WorkflowState.for_plan(kernel_run_id, plan)
        save_plan = getattr(state_store, "save_plan", None)
        if callable(save_plan):
            save_plan(kernel_run_id, plan)

    tail_adapters = _ReportingTailAdapters(tail_runner, workflow_id)
    module_tools = {
        f"execute-module-lane-{module_id}": (
            lambda value, current_module_id=module_id: module_runtime.execute_lane(
                current_module_id,
                value,
                workflow_id,
            )
        )
        for module_id in REPORT_MODULE_IDS
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
                    "run-reporting-cross": tail_adapters.cross,
                    "run-reporting-chief": tail_adapters.chief,
                    "run-reporting-final": tail_adapters.final,
                    "run-reporting-delivery": tail_adapters.delivery,
                },
                contracts=build_contract_catalog(definitions),
                definitions=definitions,
                subworkflows={
                    cohort.id: cohort_plan,
                    tail.id: tail_plan,
                },
            ),
        )
    except BaseException:
        current = (
            tail_adapters.current_state
            or module_runtime.current_state
            or state
        )
        state.clear()
        state.update(current)
        raise
    state.clear()
    state.update(completed.outputs["result"])
    return completed


class _BatchModuleRuntime:
    """Compatibility adapter for callers that still provide one batch callback."""

    def __init__(
        self,
        execute_current: Callable[
            [tuple[str, ...], dict[str, Any], str], Awaitable[None]
        ],
        requested_modules: tuple[str, ...],
        state: dict[str, Any],
        workflow_id: str,
    ) -> None:
        self._execute_current = execute_current
        self._requested_modules = requested_modules
        self.current_state = deepcopy(state)
        self._workflow_id = workflow_id

    async def prepare_lanes(self, state: dict[str, Any]) -> dict[str, Any]:
        return state

    async def execute_lane(
        self,
        module_id: str,
        _state: dict[str, Any],
        _workflow_id: str,
    ) -> DeclarativeModuleLaneOutcome:
        return DeclarativeModuleLaneOutcome(
            module_id=module_id,
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

    async def prepare_lanes(self, state: dict[str, Any]) -> dict[str, Any]:
        return state

    async def execute_lane(
        self,
        module_id: str,
        lane_state: dict[str, Any],
        workflow_id: str,
    ) -> DeclarativeModuleLaneOutcome:
        if module_id not in self._requested_modules:
            return DeclarativeModuleLaneOutcome(
                module_id=module_id,
                status="completed",
            )
        try:
            self._runner._raise_if_cancel_requested(lane_state["run_id"])
            recovered_lanes = self._runner._recovery_store(
                lane_state
            ).load_completed_lanes(
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
                recovered = await self._runner._execute_module_lane(
                    module_id,
                    lane_state,
                    workflow_id,
                    defer_main_exceptions=True,
                )
        except asyncio.CancelledError:
            raise
        except DeferredMainDecision as exc:
            deferred_state = getattr(exc, "lane_state", lane_state)
            return DeclarativeModuleLaneOutcome(
                module_id=module_id,
                status="deferred",
                error=str(exc),
                lane_state={"reporting_state": deferred_state},
            )
        except BaseException as exc:
            self._failures[module_id] = exc
            return DeclarativeModuleLaneOutcome(
                module_id=module_id,
                status="failed",
                error=str(exc),
            )
        if recovered is None:
            module = lane_state["module_submissions"][module_id]
            return DeclarativeModuleLaneOutcome(
                module_id=module_id,
                status="completed",
                module=module,
                lane_state={"reporting_state": lane_state},
            )
        submission, completion_ref, completion, completed_lane_state = recovered
        return DeclarativeModuleLaneOutcome(
            module_id=module_id,
            status="completed",
            module=submission,
            lane_state={"reporting_state": completed_lane_state},
            completion_ref=completion_ref,
            completion=completion.model_dump(mode="json"),
        )

    async def reduce_lanes(
        self,
        values: Mapping[str, Any],
    ) -> dict[str, Any]:
        outcomes = {
            module_id: DeclarativeModuleLaneOutcome.model_validate(values[module_id])
            for module_id in REPORT_MODULE_IDS
        }
        results: dict[
            str, tuple[Any, str, LaneCompletion, dict[str, Any]]
        ] = {}
        for module_id in self._requested_modules:
            outcome = outcomes[module_id]
            if outcome.status == "deferred":
                resumed = (outcome.lane_state or {}).get("reporting_state")
                if isinstance(resumed, dict):
                    resumed["resume"] = True
                try:
                    submission, completion_ref, completion, lane_state = (
                        await self._runner._execute_module_lane(
                            module_id,
                            self.current_state,
                            self._workflow_id,
                            defer_main_exceptions=False,
                            lane_state_override=resumed,
                        )
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
                            self.current_state.setdefault(key, {}).update(
                                lane_state[key]
                            )
                        elif isinstance(lane_state[key], list):
                            current = self.current_state.setdefault(key, [])
                            current.extend(
                                item for item in lane_state[key] if item not in current
                            )
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
            if outcomes[module_id].status == "failed"
            or module_id in self._failures
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
]

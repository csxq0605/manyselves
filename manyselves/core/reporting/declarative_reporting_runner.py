"""Explicit declarative path over the complete current Reporting lifecycle."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from copy import deepcopy
from typing import Any

from manyselves.capabilities.distribution_reporting import (
    load_distribution_reporting_capability,
)
from manyselves.kernel.contracts import build_contract_catalog
from manyselves.kernel.definitions import (
    DefinitionKind,
    DefinitionRegistry,
    WorkflowDefinition,
)
from manyselves.kernel.executors import RuntimeContext, build_builtin_executor_registry
from manyselves.kernel.ports import WorkflowStateStore
from manyselves.kernel.workflow import WorkflowCompiler, WorkflowState
from manyselves.runtime.state_store import FileWorkflowStateStore
from manyselves.runtime.workflow_host import (
    InMemoryWorkflowEventSink,
    WorkflowRuntimeHost,
)

from .declarative_reporting_tail import _ReportingTailAdapters
from .models import REPORT_MODULE_IDS
from .workflow import ReportWorkflowRunner


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
    execute_current: Callable[[tuple[str, ...], dict[str, Any], str], Awaitable[None]],
    requested_modules: tuple[str, ...],
    state: dict[str, Any],
    workflow_id: str,
    state_store: WorkflowStateStore,
    tail_runner: Any | None = None,
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
    except FileNotFoundError:
        kernel_state = WorkflowState.for_plan(kernel_run_id, plan)
        save_plan = getattr(state_store, "save_plan", None)
        if callable(save_plan):
            save_plan(kernel_run_id, plan)

    current_state = deepcopy(state)

    async def run_current(value: dict[str, Any]) -> dict[str, Any]:
        nonlocal current_state
        current_state = value
        await execute_current(requested_modules, value, workflow_id)
        return value

    tail_adapters = _ReportingTailAdapters(tail_runner, workflow_id)
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
                    "run-reporting-module-work": run_current,
                    "run-reporting-cross": tail_adapters.cross,
                    "run-reporting-chief": tail_adapters.chief,
                    "run-reporting-final": tail_adapters.final,
                    "run-reporting-delivery": tail_adapters.delivery,
                },
                contracts=build_contract_catalog(definitions),
                definitions=definitions,
                subworkflows={tail.id: tail_plan},
            ),
        )
    except BaseException:
        current = tail_adapters.current_state or current_state
        state.clear()
        state.update(current)
        raise
    state.clear()
    state.update(completed.outputs["result"])
    return completed


class DeclarativeReportWorkflowRunner(ReportWorkflowRunner):
    """Select declarative stage orchestration without replacing current semantics."""

    async def _run_module_lanes(
        self,
        requested_modules: tuple[str, ...],
        state: dict,
        workflow_id: str,
    ) -> None:
        async def execute_current(
            modules: tuple[str, ...],
            current_state: dict[str, Any],
            current_workflow_id: str,
        ) -> None:
            await ReportWorkflowRunner._run_module_lanes(
                self,
                modules,
                current_state,
                current_workflow_id,
            )

        await execute_declarative_module_stage(
            execute_current=execute_current,
            requested_modules=requested_modules,
            state=state,
            workflow_id=workflow_id,
            state_store=FileWorkflowStateStore(self.service.workspace),
            tail_runner=_CurrentTailStages(self),
        )


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

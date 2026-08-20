"""Explicit declarative path over the complete current Reporting lifecycle."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from manyselves.kernel.definitions import (
    ContractDefinition,
    DefinitionRegistry,
    ToolDefinition,
    WorkflowDefinition,
)
from manyselves.kernel.executors import (
    ControlFlowWorkflowExecutor,
    RuntimeContext,
    build_builtin_executor_registry,
)
from manyselves.kernel.ports import WorkflowStateStore
from manyselves.kernel.workflow import WorkflowCompiler, WorkflowState
from manyselves.runtime.state_store import FileWorkflowStateStore

from .declarative_reporting_tail import execute_declarative_reporting_tail
from .workflow import ReportWorkflowRunner


def build_reporting_module_stage_definition(
) -> tuple[DefinitionRegistry, WorkflowDefinition]:
    """Build the neutral stage boundary around the complete current cohort."""

    registry = DefinitionRegistry()
    token = ContractDefinition(
        id="reporting-run-token",
        version="1.0.0",
        description="Opaque run token for a Capability-owned Reporting stage",
        adapter="json_schema",
        schema={"type": "string"},
    )
    registry.register(token)
    registry.register(
        ToolDefinition(
            id="run-current-module-cohort",
            version="1.0.0",
            description="Capability adapter for the complete current module cohort",
            implementation="capability:distribution-reporting:module-cohort",
            input_contract=token.id,
            output_contract=token.id,
            side_effect="ordered_state",
            parallel_safe=False,
        )
    )
    workflow = WorkflowDefinition(
        id="distribution-reporting-module-stage",
        version="1.0.0",
        description="Execute or resume the complete current module cohort",
        output_contract=token.id,
        state={},
        actions=[
            {
                "id": "run-module-cohort",
                "kind": "invoke_tool",
                "tool": "run-current-module-cohort",
                "input_variable": "run-token",
                "output_variable": "run-token",
            },
            {
                "id": "finish-module-stage",
                "kind": "end_workflow",
                "output_variable": "run-token",
                "output_name": "result",
            },
        ],
    )
    return registry, workflow


async def execute_declarative_module_stage(
    *,
    execute_current: Callable[[tuple[str, ...], dict[str, Any], str], Awaitable[None]],
    requested_modules: tuple[str, ...],
    state: dict[str, Any],
    workflow_id: str,
    state_store: WorkflowStateStore,
) -> WorkflowState:
    """Run current module semantics behind a persisted neutral Tool action."""

    definitions, workflow = build_reporting_module_stage_definition()
    workflow.state = {"run-token": str(state["run_id"])}
    executors = build_builtin_executor_registry()
    plan = WorkflowCompiler(executors).compile(workflow, definitions)
    kernel_run_id = f"{state['run_id']}--reporting-module-stage"
    try:
        kernel_state = state_store.load(kernel_run_id)
        kernel_state.variables["run-token"] = str(state["run_id"])
    except FileNotFoundError:
        kernel_state = WorkflowState.for_plan(kernel_run_id, plan)
        save_plan = getattr(state_store, "save_plan", None)
        if callable(save_plan):
            save_plan(kernel_run_id, plan)

    async def run_current(_run_token: str) -> str:
        await execute_current(requested_modules, state, workflow_id)
        return str(state["run_id"])

    return await ControlFlowWorkflowExecutor(executors, state_store).execute(
        plan,
        kernel_state,
        RuntimeContext(tools={"run-current-module-cohort": run_current}),
    )


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
        )

    async def _cross_review(self, state: dict, workflow_id: str) -> None:
        await execute_declarative_reporting_tail(
            runner=_CurrentTailStages(self),
            state=state,
            workflow_id=workflow_id,
            state_store=FileWorkflowStateStore(self.service.workspace),
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

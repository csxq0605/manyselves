"""Declarative Reporting tail bound to the current stage implementations."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from typing import Any

from manyselves.kernel.contracts import ContractAdapter, build_contract_adapter
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
from manyselves.kernel.workflow import WorkflowCompiler, WorkflowState, WorkflowStatus

from .models import REPORT_MODULE_IDS


class DeclarativeReportingTailError(RuntimeError):
    """Raised when the Reporting tail does not reach delivery completion."""


def build_reporting_tail_definition(
) -> tuple[DefinitionRegistry, dict[str, ContractAdapter], WorkflowDefinition]:
    """Build the Reporting-owned Cross through Delivery stage sequence."""

    registry = DefinitionRegistry()
    state_contract = ContractDefinition(
        id="reporting_tail_state",
        version="1.0.0",
        description="Current typed Reporting state carried between tail stages",
        adapter="json_schema",
        schema={"type": "object"},
    )
    registry.register(state_contract)
    contracts = {state_contract.id: build_contract_adapter(state_contract)}
    stages = ("cross", "chief", "final", "delivery")
    for stage in stages:
        registry.register(
            ToolDefinition(
                id=f"run-reporting-{stage}",
                version="1.0.0",
                description=f"Bind the current Reporting {stage} stage",
                implementation=f"capability:reporting-{stage}",
                input_contract=state_contract.id,
                output_contract=state_contract.id,
                side_effect="ordered_state",
                parallel_safe=False,
            )
        )
    workflow = WorkflowDefinition(
        id="distribution-reporting-tail",
        version="1.0.0",
        description="Cross, Chief, Final, Render and Delivery in current order",
        output_contract=state_contract.id,
        state={},
        actions=[
            *(
                {
                    "id": f"run-{stage}",
                    "kind": "invoke_tool",
                    "tool": f"run-reporting-{stage}",
                    "input_variable": "reporting-state",
                    "output_variable": "reporting-state",
                }
                for stage in stages
            ),
            {
                "id": "finish-reporting-tail",
                "kind": "end_workflow",
                "output_variable": "reporting-state",
                "output_name": "result",
            },
        ],
    )
    return registry, contracts, workflow


async def execute_declarative_reporting_tail(
    *,
    runner: Any,
    state: dict[str, Any],
    workflow_id: str,
    state_store: WorkflowStateStore,
) -> WorkflowState:
    """Run current tail stages through neutral actions without changing default routing."""

    definitions, contracts, workflow = build_reporting_tail_definition()
    workflow.state = {"reporting-state": deepcopy(state)}
    executors = build_builtin_executor_registry()
    plan = WorkflowCompiler(executors).compile(workflow, definitions)
    kernel_state = WorkflowState.for_plan(
        f"{state['run_id']}--reporting-tail",
        plan,
    )
    adapters = _ReportingTailAdapters(runner, workflow_id)
    try:
        completed = await ControlFlowWorkflowExecutor(executors, state_store).execute(
            plan,
            kernel_state,
            RuntimeContext(
                tools={
                    "run-reporting-cross": adapters.cross,
                    "run-reporting-chief": adapters.chief,
                    "run-reporting-final": adapters.final,
                    "run-reporting-delivery": adapters.delivery,
                },
                contracts=contracts,
                definitions=definitions,
            ),
        )
    except BaseException:
        _replace_state(state, kernel_state.variables["reporting-state"])
        raise
    _replace_state(state, completed.outputs["result"])
    if (
        completed.status is not WorkflowStatus.COMPLETED
        or "delivery_completion_ref" not in state
    ):
        raise DeclarativeReportingTailError("declarative Reporting tail did not deliver")
    return completed


class _ReportingTailAdapters:
    def __init__(self, runner: Any, workflow_id: str) -> None:
        self._runner = runner
        self._workflow_id = workflow_id

    async def cross(self, state: dict[str, Any]) -> dict[str, Any]:
        if "cross_review_completion_ref" not in state:
            await self._runner._cross_review(state, self._workflow_id)
        return state

    async def chief(self, state: dict[str, Any]) -> dict[str, Any]:
        if (
            "final_review_completion_ref" not in state
            and "chief_candidate_ref" not in state
        ):
            await self._runner._chief_edit(state, self._workflow_id)
        return state

    async def final(self, state: dict[str, Any]) -> dict[str, Any]:
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
        if "delivery_completion_ref" not in state:
            self._runner._deliver(state)
        return state


def _replace_state(target: dict[str, Any], value: Mapping[str, Any]) -> None:
    target.clear()
    target.update(value)

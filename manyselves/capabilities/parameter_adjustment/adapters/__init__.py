"""Capability-owned adapters for the neutral parameter-adjustment workflow."""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from manyselves.kernel.contracts import ContractAdapter, build_contract_adapter
from manyselves.kernel.definitions import ContractDefinition, DefinitionKind, WorkflowDefinition
from manyselves.kernel.executors import (
    ControlFlowWorkflowExecutor,
    RuntimeContext,
    build_builtin_executor_registry,
)
from manyselves.kernel.ports import AgentInvocationOutcome
from manyselves.kernel.workflow import WorkflowCompiler, WorkflowState
from manyselves.runtime.state_store import FileWorkflowStateStore


@dataclass(frozen=True, slots=True)
class ParameterAdjustmentResult:
    """Completed neutral state plus observable Capability adapter activity."""

    state: WorkflowState
    agent_calls: int


class _ParameterAdjuster:
    def __init__(self) -> None:
        self.calls = 0

    async def invoke(
        self,
        _agent: Any,
        _task: Any,
        _value: Any,
        _conversation: Any,
        *,
        task_id: str,
    ) -> AgentInvocationOutcome:
        self.calls += 1
        return AgentInvocationOutcome(result={"value": 10})


async def execute_parameter_adjustment(
    *,
    workspace: Path,
    run_id: str,
    values: dict[str, Any],
) -> ParameterAdjustmentResult:
    """Compile and execute the packaged workflow without Reporting dependencies."""

    from .. import load_parameter_adjustment_capability

    _capability, registry = load_parameter_adjustment_capability()
    workflow = registry.require(DefinitionKind.WORKFLOW, "parameter-adjustment")
    if not isinstance(workflow, WorkflowDefinition):
        raise TypeError("parameter-adjustment definition is not a workflow")

    contracts: dict[str, ContractAdapter] = {}
    for definition in registry.all(DefinitionKind.CONTRACT):
        if isinstance(definition, ContractDefinition):
            contracts[definition.id] = build_contract_adapter(definition)
    parameters = contracts["parameter-input"].validate(values)
    workflow = workflow.model_copy(deep=True)
    workflow.state = {"parameters": parameters}

    executors = build_builtin_executor_registry()
    plan = WorkflowCompiler(executors).compile(workflow, registry)
    store = FileWorkflowStateStore(workspace)
    try:
        state = store.load(run_id)
    except FileNotFoundError:
        state = WorkflowState.for_plan(run_id, plan)
        store.save_plan(run_id, plan)

    adjuster = _ParameterAdjuster()
    completed = await ControlFlowWorkflowExecutor(executors, store).execute(
        plan,
        state,
        RuntimeContext(
            tools={"normalize-parameter": lambda value: value["value"]},
            contracts=contracts,
            agents={"parameter-adjuster": adjuster},
            definitions=registry,
        ),
    )
    return ParameterAdjustmentResult(state=completed, agent_calls=adjuster.calls)


__all__ = ["ParameterAdjustmentResult", "execute_parameter_adjustment"]

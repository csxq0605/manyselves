from pathlib import Path

import pytest

from manyselves.kernel.contracts import build_contract_adapter
from manyselves.kernel.definitions import (
    ContractDefinition,
    DefinitionRegistry,
    OutputDefinition,
    ToolDefinition,
    WorkflowDefinition,
)
from manyselves.kernel.executors import RuntimeContext, build_builtin_executor_registry
from manyselves.kernel.workflow import (
    StartWorkflow,
    StatelessWorkflowKernel,
    WorkflowCompiler,
    WorkflowState,
    WorkflowStatus,
)
from manyselves.runtime.state_store import FileWorkflowStateStore
from manyselves.runtime.workflow_host import (
    InMemoryWorkflowEventSink,
    WorkflowRuntimeHost,
)


def _workflow():
    registry = DefinitionRegistry()
    number = ContractDefinition(
        id="number",
        version="1.0.0",
        description="One integer",
        adapter="json_schema",
        schema={"type": "integer"},
    )
    registry.register(number)
    registry.register(
        ToolDefinition(
            id="double",
            version="1.0.0",
            description="Double one integer",
            implementation="fixture:double",
            input_contract="number",
            output_contract="number",
        )
    )
    registry.register(
        OutputDefinition(
            id="result-value",
            version="1.0.0",
            description="Doubled result",
            contract="number",
            label="Result",
        )
    )
    workflow = WorkflowDefinition(
        id="stateless-sequence",
        version="1.0.0",
        description="Stateless kernel fixture",
        outputs=["result-value"],
        state={"input": 4},
        actions=[
            {
                "id": "double",
                "kind": "invoke_tool",
                "tool": "double",
                "input_variable": "input",
                "output_variable": "doubled",
            },
            {
                "id": "publish",
                "kind": "publish_result",
                "output": "result-value",
                "input_variable": "doubled",
                "output_name": "result",
            },
            {
                "id": "finish",
                "kind": "end_workflow",
                "output_variable": "doubled",
                "output_name": "result",
            },
        ],
    )
    registry.register(workflow)
    executors = build_builtin_executor_registry()
    plan = WorkflowCompiler(executors).compile(workflow, registry)
    contracts = {number.id: build_contract_adapter(number)}
    return registry, executors, plan, contracts


def test_stateless_kernel_returns_a_transition_without_mutating_input_state() -> None:
    _registry, _executors, plan, _contracts = _workflow()
    state = WorkflowState.for_plan("pure-run", plan)

    transition = StatelessWorkflowKernel().transition(plan, state, StartWorkflow())

    assert state.status is WorkflowStatus.PENDING
    assert state.actions["double"].status == "pending"
    assert transition.state.status is WorkflowStatus.RUNNING
    assert transition.state.actions["double"].status == "running"
    assert [effect.action_id for effect in transition.effects] == ["double"]


@pytest.mark.asyncio
async def test_runtime_host_executes_effects_persists_events_and_reuses_completion(
    tmp_path: Path,
) -> None:
    registry, executors, plan, contracts = _workflow()
    calls = 0

    def double(value: int) -> int:
        nonlocal calls
        calls += 1
        return value * 2

    store = FileWorkflowStateStore(tmp_path)
    events = InMemoryWorkflowEventSink()
    host = WorkflowRuntimeHost(executors, store, events)
    initial = WorkflowState.for_plan("host-run", plan)
    context = RuntimeContext(
        tools={"double": double},
        contracts=contracts,
        definitions=registry,
    )

    completed = await host.execute(plan, initial, context)
    repeated = await host.execute(plan, store.load("host-run"), context)

    assert completed.status is WorkflowStatus.COMPLETED
    assert completed.outputs == {"result": 8}
    assert repeated.outputs == {"result": 8}
    assert calls == 1
    assert [event.kind for event in events.events] == [
        "workflow.started",
        "action.started",
        "action.completed",
        "action.started",
        "output.published",
        "action.completed",
        "action.started",
        "action.completed",
        "workflow.completed",
    ]

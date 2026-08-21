import asyncio
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
    retry_parallel_branches,
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


@pytest.mark.asyncio
async def test_runtime_host_joins_parallel_branches_inside_one_run_state(
    tmp_path: Path,
) -> None:
    registry, executors, _plan, contracts = _workflow()
    workflow = WorkflowDefinition(
        id="host-parallel",
        version="1.0.0",
        description="Parallel host fixture",
        state={"left-input": 1, "right-input": 2},
        actions=[
            {
                "id": "parallel",
                "kind": "parallel",
                "branches": {"left": "left", "right": "right"},
                "join": "join",
            },
            {
                "id": "left",
                "kind": "invoke_tool",
                "tool": "double",
                "input_variable": "left-input",
                "output_variable": "left-output",
            },
            {"id": "left-done", "kind": "goto", "target": "join"},
            {
                "id": "right",
                "kind": "invoke_tool",
                "tool": "double",
                "input_variable": "right-input",
                "output_variable": "right-output",
            },
            {"id": "right-done", "kind": "goto", "target": "join"},
            {
                "id": "join",
                "kind": "join",
                "parallel": "parallel",
                "inputs": {"left": "left-output", "right": "right-output"},
                "output_variable": "joined",
            },
            {
                "id": "finish",
                "kind": "end_workflow",
                "output_variable": "joined",
            },
        ],
    )
    registry.register(workflow)
    plan = WorkflowCompiler(executors).compile(workflow, registry)
    active = 0
    calls = 0
    maximum_active = 0

    async def double(value: int) -> int:
        nonlocal active, calls, maximum_active
        calls += 1
        active += 1
        maximum_active = max(maximum_active, active)
        await asyncio.sleep(0)
        active -= 1
        return value * 2

    store = FileWorkflowStateStore(tmp_path)
    host = WorkflowRuntimeHost(
        executors,
        store,
        InMemoryWorkflowEventSink(),
    )
    context = RuntimeContext(
        tools={"double": double},
        contracts=contracts,
        definitions=registry,
    )
    completed = await host.execute(
        plan,
        WorkflowState.for_plan("parallel-host-run", plan),
        context,
    )

    assert maximum_active == 2
    assert calls == 2
    assert completed.outputs == {"result": {"left": 2, "right": 4}}
    assert set(completed.parallel_states["parallel"]) == {"left", "right"}
    resumed = retry_parallel_branches(
        plan,
        completed,
        parallel_action_id="parallel",
        branch_ids={"left"},
    )
    assert completed.status is WorkflowStatus.COMPLETED
    assert set(completed.parallel_states["parallel"]) == {"left", "right"}
    assert set(resumed.parallel_states["parallel"]) == {"right"}

    recovered = await host.execute(plan, resumed, context)

    assert recovered.outputs == {"result": {"left": 2, "right": 4}}
    assert calls == 3
    run_directories = [
        path.name for path in (tmp_path / "Work" / "runs").iterdir()
    ]
    assert run_directories == ["parallel-host-run"]


@pytest.mark.asyncio
async def test_runtime_host_nests_subworkflow_state_in_the_parent_run(
    tmp_path: Path,
) -> None:
    registry, executors, _plan, contracts = _workflow()
    child = WorkflowDefinition(
        id="host-child",
        version="1.0.0",
        description="Child fixture",
        state={"input": 0},
        actions=[
            {
                "id": "child-double",
                "kind": "invoke_tool",
                "tool": "double",
                "input_variable": "input",
                "output_variable": "doubled",
            },
            {
                "id": "child-finish",
                "kind": "end_workflow",
                "output_variable": "doubled",
            },
        ],
    )
    parent = WorkflowDefinition(
        id="host-parent",
        version="1.0.0",
        description="Parent fixture",
        state={"value": 4},
        actions=[
            {
                "id": "call-child",
                "kind": "subworkflow",
                "workflow": child.id,
                "input_variable": "value",
                "child_input_variable": "input",
                "child_output_name": "result",
                "output_variable": "child-result",
            },
            {
                "id": "parent-finish",
                "kind": "end_workflow",
                "output_variable": "child-result",
            },
        ],
    )
    registry.register(child)
    registry.register(parent)
    child_plan = WorkflowCompiler(executors).compile(child, registry)
    parent_plan = WorkflowCompiler(executors).compile(parent, registry)
    store = FileWorkflowStateStore(tmp_path)
    events = InMemoryWorkflowEventSink()

    completed = await WorkflowRuntimeHost(
        executors,
        store,
        events,
    ).execute(
        parent_plan,
        WorkflowState.for_plan("subworkflow-host-run", parent_plan),
        RuntimeContext(
            tools={"double": lambda value: value * 2},
            contracts=contracts,
            definitions=registry,
            subworkflows={child.id: child_plan},
        ),
    )

    assert completed.outputs == {"result": 8}
    assert completed.subworkflow_states["call-child"]["status"] == "completed"
    assert [
        (event.kind, event.workflow_id, event.action_id)
        for event in events.events
        if event.workflow_id == "host-child"
    ] == [
        ("workflow.started", "host-child", None),
        ("action.started", "host-child", "child-double"),
        ("action.completed", "host-child", "child-double"),
        ("action.started", "host-child", "child-finish"),
        ("action.completed", "host-child", "child-finish"),
        ("workflow.completed", "host-child", None),
    ]
    assert [path.name for path in (tmp_path / "Work" / "runs").iterdir()] == [
        "subworkflow-host-run"
    ]


@pytest.mark.asyncio
async def test_runtime_host_resumes_only_the_failed_subworkflow_action(
    tmp_path: Path,
) -> None:
    registry, executors, _plan, contracts = _workflow()
    for tool_id in ("first-step", "flaky-step"):
        registry.register(
            ToolDefinition(
                id=tool_id,
                version="1.0.0",
                description=tool_id,
                implementation=f"fixture:{tool_id}",
                input_contract="number",
                output_contract="number",
            )
        )
    child = WorkflowDefinition(
        id="recoverable-child",
        version="1.0.0",
        description="Child with one recoverable failure",
        state={"input": 1},
        actions=[
            {
                "id": "first",
                "kind": "invoke_tool",
                "tool": "first-step",
                "input_variable": "input",
                "output_variable": "first-result",
            },
            {
                "id": "flaky",
                "kind": "invoke_tool",
                "tool": "flaky-step",
                "input_variable": "first-result",
                "output_variable": "final-result",
            },
            {
                "id": "child-finish",
                "kind": "end_workflow",
                "output_variable": "final-result",
            },
        ],
    )
    parent = WorkflowDefinition(
        id="recoverable-parent",
        version="1.0.0",
        description="Parent preserving child progress",
        state={"value": 1},
        actions=[
            {
                "id": "call-child",
                "kind": "subworkflow",
                "workflow": child.id,
                "input_variable": "value",
                "child_input_variable": "input",
                "child_output_name": "result",
                "output_variable": "child-result",
            },
            {
                "id": "parent-finish",
                "kind": "end_workflow",
                "output_variable": "child-result",
            },
        ],
    )
    registry.register(child)
    registry.register(parent)
    child_plan = WorkflowCompiler(executors).compile(child, registry)
    parent_plan = WorkflowCompiler(executors).compile(parent, registry)
    calls = {"first": 0, "flaky": 0}

    def first(value: int) -> int:
        calls["first"] += 1
        return value + 1

    def flaky(value: int) -> int:
        calls["flaky"] += 1
        if calls["flaky"] == 1:
            raise RuntimeError("injected child failure")
        return value * 2

    store = FileWorkflowStateStore(tmp_path)
    host = WorkflowRuntimeHost(executors, store, InMemoryWorkflowEventSink())
    context = RuntimeContext(
        tools={"first-step": first, "flaky-step": flaky},
        contracts=contracts,
        definitions=registry,
        subworkflows={child.id: child_plan},
    )

    with pytest.raises(RuntimeError, match="injected child failure"):
        await host.execute(
            parent_plan,
            WorkflowState.for_plan("recoverable-run", parent_plan),
            context,
        )

    failed = store.load("recoverable-run")
    assert failed.subworkflow_states["call-child"]["actions"]["first"]["status"] == (
        "completed"
    )
    assert failed.subworkflow_states["call-child"]["actions"]["flaky"]["status"] == (
        "failed"
    )

    completed = await host.execute(parent_plan, failed, context)

    assert completed.outputs == {"result": 4}
    assert calls == {"first": 1, "flaky": 2}

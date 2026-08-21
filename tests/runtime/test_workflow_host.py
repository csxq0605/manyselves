import asyncio
from pathlib import Path

import pytest

from manyselves.kernel.contracts import build_contract_adapter
from manyselves.kernel.definitions import (
    ContractDefinition,
    DefinitionRegistry,
    InteractionDefinition,
    OutputDefinition,
    ToolDefinition,
    WorkflowDefinition,
)
from manyselves.kernel.executors import RuntimeContext, build_builtin_executor_registry
from manyselves.kernel.workflow import (
    ActionExecutionStatus,
    StartWorkflow,
    StatelessWorkflowKernel,
    WorkflowCompiler,
    WorkflowState,
    WorkflowStatus,
    resume_waiting_input,
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
async def test_runtime_host_executes_explicit_exit_loop_without_iteration_cap(
    tmp_path: Path,
) -> None:
    registry, executors, _plan, contracts = _workflow()
    workflow = WorkflowDefinition(
        id="host-exit-loop",
        version="1.0.0",
        description="Runtime Host exits a condition-controlled loop",
        state={"count": 1},
        actions=[
            {
                "id": "double",
                "kind": "invoke_tool",
                "tool": "double",
                "input_variable": "count",
                "output_variable": "count",
            },
            {
                "id": "check",
                "kind": "if",
                "condition": {"variable": "count", "operator": "lt", "value": 8},
                "then": "repeat",
                "otherwise": "finish",
            },
            {"id": "repeat", "kind": "goto", "target": "double"},
            {"id": "finish", "kind": "end_workflow", "output_variable": "count"},
        ],
    )
    registry.register(workflow)
    plan = WorkflowCompiler(executors).compile(workflow, registry)
    events = InMemoryWorkflowEventSink()

    completed = await WorkflowRuntimeHost(
        executors,
        FileWorkflowStateStore(tmp_path),
        events,
    ).execute(
        plan,
        WorkflowState.for_plan("host-exit-loop-run", plan),
        RuntimeContext(
            tools={"double": lambda value: value * 2},
            contracts=contracts,
            definitions=registry,
        ),
    )

    assert completed.status is WorkflowStatus.COMPLETED
    assert completed.outputs == {"result": 8}
    assert events.events[-1].kind == "workflow.completed"


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
async def test_subworkflow_binds_multiple_named_parent_variables(
    tmp_path: Path,
) -> None:
    registry, executors, _plan, contracts = _workflow()
    child = WorkflowDefinition(
        id="named-input-child",
        version="1.0.0",
        description="Child with two named inputs",
        state={"left": 0, "right": 0},
        actions=[
            {
                "id": "add-values",
                "kind": "invoke_tool",
                "tool": "double",
                "input_variables": {"left": "left", "right": "right"},
                "output_variable": "total",
            },
            {
                "id": "finish-child",
                "kind": "end_workflow",
                "output_variable": "total",
            },
        ],
    )
    parent = WorkflowDefinition(
        id="named-input-parent",
        version="1.0.0",
        description="Parent binding two child variables",
        state={"first": 3, "second": 5},
        actions=[
            {
                "id": "call-child",
                "kind": "subworkflow",
                "workflow": child.id,
                "input_variables": {"left": "first", "right": "second"},
                "child_output_name": "result",
                "output_variable": "child-result",
            },
            {
                "id": "finish-parent",
                "kind": "end_workflow",
                "output_variable": "child-result",
            },
        ],
    )
    registry.register(child)
    registry.register(parent)
    child_plan = WorkflowCompiler(executors).compile(child, registry)
    parent_plan = WorkflowCompiler(executors).compile(parent, registry)

    completed = await WorkflowRuntimeHost(
        executors,
        FileWorkflowStateStore(tmp_path),
        InMemoryWorkflowEventSink(),
    ).execute(
        parent_plan,
        WorkflowState.for_plan("named-subworkflow-run", parent_plan),
        RuntimeContext(
            tools={"double": lambda values: values["left"] + values["right"]},
            contracts=contracts,
            definitions=registry,
            subworkflows={child.id: child_plan},
        ),
    )

    assert completed.outputs == {"result": 8}
    assert completed.subworkflow_states["call-child"]["variables"]["left"] == 3
    assert completed.subworkflow_states["call-child"]["variables"]["right"] == 5


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


def _register_text_interaction(
    registry: DefinitionRegistry,
    *,
    contract_id: str,
    interaction_id: str,
) -> dict[str, object]:
    registry.register(
        ContractDefinition(
            id=contract_id,
            version="1.0.0",
            description="One text response",
            adapter="json_schema",
            schema={"type": "string"},
        )
    )
    registry.register(
        InteractionDefinition(
            id=interaction_id,
            version="1.0.0",
            description="Collect one text response",
            input_contract=contract_id,
            title="Text response",
        )
    )
    return {contract_id: build_contract_adapter(registry.require("contract", contract_id))}


@pytest.mark.asyncio
async def test_runtime_host_waits_and_resumes_input_inside_subworkflow(
    tmp_path: Path,
) -> None:
    registry, executors, _plan, contracts = _workflow()
    contracts.update(
        _register_text_interaction(
            registry,
            contract_id="child-answer",
            interaction_id="child-interaction",
        )
    )
    child = WorkflowDefinition(
        id="waiting-child",
        version="1.0.0",
        description="Child that asks for one response",
        state={"input": "seed"},
        actions=[
            {
                "id": "ask-child",
                "kind": "request_input",
                "interaction": "child-interaction",
                "output_variable": "answer",
            },
            {
                "id": "finish-child",
                "kind": "end_workflow",
                "output_variable": "answer",
            },
        ],
    )
    parent = WorkflowDefinition(
        id="waiting-parent",
        version="1.0.0",
        description="Parent containing a waiting child",
        state={"seed": 2},
        actions=[
            {
                "id": "before-child",
                "kind": "invoke_tool",
                "tool": "double",
                "input_variable": "seed",
                "output_variable": "observed",
            },
            {
                "id": "call-child",
                "kind": "subworkflow",
                "workflow": child.id,
                "input_variable": "observed",
                "child_input_variable": "input",
                "child_output_name": "result",
                "output_variable": "child-result",
            },
            {
                "id": "finish-parent",
                "kind": "end_workflow",
                "output_variable": "child-result",
            },
        ],
    )
    registry.register(child)
    registry.register(parent)
    child_plan = WorkflowCompiler(executors).compile(child, registry)
    parent_plan = WorkflowCompiler(executors).compile(parent, registry)
    calls = 0

    def double(value: int) -> int:
        nonlocal calls
        calls += 1
        return value * 2

    events = InMemoryWorkflowEventSink()
    store = FileWorkflowStateStore(tmp_path)
    host = WorkflowRuntimeHost(executors, store, events)
    context = RuntimeContext(
        tools={"double": double},
        contracts=contracts,
        definitions=registry,
        subworkflows={child.id: child_plan},
    )

    waiting = await host.execute(
        parent_plan,
        WorkflowState.for_plan("nested-input-run", parent_plan),
        context,
    )

    assert waiting.status is WorkflowStatus.WAITING
    assert waiting.waiting_input is not None
    assert waiting.waiting_input["input_id"] == "ask-child"
    assert waiting.waiting_input["path"] == [
        {
            "kind": "subworkflow",
            "action_id": "call-child",
            "workflow_id": child.id,
        }
    ]
    assert waiting.subworkflow_states["call-child"]["status"] == "waiting"
    assert waiting.subworkflow_states["call-child"]["actions"]["ask-child"][
        "status"
    ] == ActionExecutionStatus.WAITING
    assert calls == 1

    resumed = resume_waiting_input(
        parent_plan,
        waiting,
        input_id="ask-child",
        values="Ada",
        contracts=contracts,
        subworkflows={child.id: child_plan},
    )
    completed = await host.execute(parent_plan, resumed, context)

    assert completed.status is WorkflowStatus.COMPLETED
    assert completed.outputs == {"result": "Ada"}
    assert completed.waiting_input is None
    assert calls == 1
    assert completed.subworkflow_states["call-child"]["variables"]["answer"] == "Ada"
    assert [
        event.action_id
        for event in events.events
        if event.kind == "action.started" and event.workflow_id == parent.id
    ].count("before-child") == 1


@pytest.mark.asyncio
async def test_runtime_host_waits_inside_parallel_child_and_reuses_completed_sibling(
    tmp_path: Path,
) -> None:
    registry, executors, _plan, contracts = _workflow()
    contracts.update(
        _register_text_interaction(
            registry,
            contract_id="branch-answer",
            interaction_id="branch-interaction",
        )
    )
    child = WorkflowDefinition(
        id="parallel-waiting-child",
        version="1.0.0",
        description="Nested branch child that asks for one response",
        state={"input": "seed"},
        actions=[
            {
                "id": "ask-branch",
                "kind": "request_input",
                "interaction": "branch-interaction",
                "output_variable": "answer",
            },
            {
                "id": "finish-branch-child",
                "kind": "end_workflow",
                "output_variable": "answer",
            },
        ],
    )
    parent = WorkflowDefinition(
        id="parallel-waiting-parent",
        version="1.0.0",
        description="Parallel parent with one waiting nested branch",
        state={"left-input": "left-seed", "right-input": 5},
        actions=[
            {
                "id": "parallel",
                "kind": "parallel",
                "branches": {"left": "left", "right": "right"},
                "join": "join",
            },
            {
                "id": "left",
                "kind": "subworkflow",
                "workflow": child.id,
                "input_variable": "left-input",
                "child_input_variable": "input",
                "child_output_name": "result",
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
    registry.register(child)
    registry.register(parent)
    child_plan = WorkflowCompiler(executors).compile(child, registry)
    parent_plan = WorkflowCompiler(executors).compile(parent, registry)
    right_calls = 0

    def double(value: int) -> int:
        nonlocal right_calls
        right_calls += 1
        return value * 2

    events = InMemoryWorkflowEventSink()
    store = FileWorkflowStateStore(tmp_path)
    host = WorkflowRuntimeHost(executors, store, events)
    context = RuntimeContext(
        tools={"double": double},
        contracts=contracts,
        definitions=registry,
        subworkflows={child.id: child_plan},
    )

    waiting = await host.execute(
        parent_plan,
        WorkflowState.for_plan("parallel-nested-input-run", parent_plan),
        context,
    )

    assert waiting.status is WorkflowStatus.WAITING
    assert waiting.waiting_input is not None
    assert waiting.waiting_input["input_id"] == "ask-branch"
    assert waiting.waiting_input["path"] == [
        {"kind": "parallel", "action_id": "parallel", "branch_id": "left"},
        {
            "kind": "subworkflow",
            "action_id": "left",
            "workflow_id": child.id,
        },
    ]
    assert waiting.parallel_results["parallel"]["right"]["right-output"] == 10
    assert waiting.parallel_states["parallel"]["right"]["status"] == "completed"
    assert waiting.parallel_states["parallel"]["left"]["status"] == "waiting"
    assert right_calls == 1

    resumed = resume_waiting_input(
        parent_plan,
        waiting,
        input_id="ask-branch",
        values="Ada",
        contracts=contracts,
        subworkflows={child.id: child_plan},
    )
    completed = await host.execute(parent_plan, resumed, context)

    assert completed.status is WorkflowStatus.COMPLETED
    assert completed.outputs == {"result": {"left": "Ada", "right": 10}}
    assert completed.parallel_results["parallel"]["right"]["right-output"] == 10
    assert completed.parallel_states["parallel"]["right"]["status"] == "completed"
    assert right_calls == 1
    assert [
        event.action_id
        for event in events.events
        if event.kind == "action.started"
        and event.workflow_id == parent.id
        and event.action_id == "right"
    ].__len__() == 1

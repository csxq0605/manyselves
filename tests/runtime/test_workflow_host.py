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
            side_effect="pure_read",
            parallel_safe=True,
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
    assert store.load_plan("host-run") == plan
    assert [event.kind for event in events.events] == [
        "workflow.started",
        "action.started",
        "tool.invoked",
        "action.completed",
        "action.started",
        "output.published",
        "action.completed",
        "action.started",
        "action.completed",
        "workflow.completed",
    ]
    assert events.events[2].data == {"tool_id": "double", "reused": False}


@pytest.mark.asyncio
async def test_failed_tool_cannot_mutate_authoritative_workflow_input(
    tmp_path: Path,
) -> None:
    registry = DefinitionRegistry()
    payload_contract = ContractDefinition(
        id="payload",
        version="1.0.0",
        description="Mutable payload",
        adapter="json_schema",
        schema={"type": "object"},
    )
    registry.register(payload_contract)
    registry.register(
        ToolDefinition(
            id="mutating-failure",
            version="1.0.0",
            description="Mutate then fail",
            implementation="fixture:mutating-failure",
            input_contract=payload_contract.id,
            output_contract=payload_contract.id,
        )
    )
    workflow = WorkflowDefinition(
        id="failed-tool-state-isolation",
        version="1.0.0",
        description="Only ActionResult may update state",
        state={"payload": {"value": 1}},
        actions=[
            {
                "id": "mutate",
                "kind": "invoke_tool",
                "tool": "mutating-failure",
                "input_variable": "payload",
                "output_variable": "result",
            },
            {
                "id": "finish",
                "kind": "end_workflow",
                "output_variable": "result",
            },
        ],
    )
    registry.register(workflow)
    executors = build_builtin_executor_registry()
    plan = WorkflowCompiler(executors).compile(workflow, registry)
    store = FileWorkflowStateStore(tmp_path)

    def mutate_then_fail(value: dict[str, int]) -> None:
        value["value"] = 99
        raise RuntimeError("injected tool failure")

    with pytest.raises(RuntimeError, match="injected tool failure"):
        await WorkflowRuntimeHost(
            executors,
            store,
            InMemoryWorkflowEventSink(),
        ).execute(
            plan,
            WorkflowState.for_plan("failed-tool-run", plan),
            RuntimeContext(
                tools={"mutating-failure": mutate_then_fail},
                contracts={
                    payload_contract.id: build_contract_adapter(payload_contract),
                },
                definitions=registry,
            ),
        )

    assert store.load("failed-tool-run").variables["payload"] == {"value": 1}


@pytest.mark.asyncio
async def test_runtime_host_resumes_with_the_run_saved_resolved_plan(
    tmp_path: Path,
) -> None:
    registry = DefinitionRegistry()
    number = ContractDefinition(
        id="number-input",
        version="1.0.0",
        description="One integer",
        adapter="json_schema",
        schema={"type": "integer"},
    )
    interaction = InteractionDefinition(
        id="number-request",
        version="1.0.0",
        description="Request one integer",
        title="Number",
        input_contract=number.id,
    )
    registry.register(number)
    registry.register(interaction)
    workflow = WorkflowDefinition(
        id="saved-plan-resume",
        version="1.0.0",
        description="Resume from one fixed compiled plan",
        interactions=[interaction.id],
        actions=[
            {
                "id": "ask-number",
                "kind": "request_input",
                "interaction": interaction.id,
                "output_variable": "answer",
            },
            {
                "id": "finish",
                "kind": "end_workflow",
                "output_variable": "answer",
                "output_name": "original",
            },
        ],
    )
    executors = build_builtin_executor_registry()
    original_plan = WorkflowCompiler(executors).compile(workflow, registry)
    store = FileWorkflowStateStore(tmp_path)
    host = WorkflowRuntimeHost(executors, store, InMemoryWorkflowEventSink())
    context = RuntimeContext(
        contracts={number.id: build_contract_adapter(number)},
        definitions=registry,
    )

    waiting = await host.execute(
        original_plan,
        WorkflowState.for_plan("fixed-plan-run", original_plan),
        context,
    )
    changed_number = number.model_copy(
        update={"schema_": {"type": "string"}}
    )
    resumed = resume_waiting_input(
        original_plan,
        waiting,
        input_id="ask-number",
        values=7,
        contracts={number.id: build_contract_adapter(changed_number)},
    )
    changed_workflow = workflow.model_copy(deep=True)
    changed_workflow.actions[-1]["output_name"] = "changed"
    changed_plan = WorkflowCompiler(executors).compile(changed_workflow, registry)

    completed = await host.execute(changed_plan, resumed, context)

    assert completed.outputs == {"original": 7}
    assert store.load_plan("fixed-plan-run") == original_plan


@pytest.mark.asyncio
async def test_runtime_host_rebinds_tools_from_the_run_saved_definition(
    tmp_path: Path,
) -> None:
    registry, executors, original_plan, contracts = _workflow()
    original_tool = registry.require("tool", "double")
    assert isinstance(original_tool, ToolDefinition)
    registry._definitions["tool"]["double"] = original_tool.model_copy(
        update={"implementation": "fixture:old-double"}
    )
    original_workflow = registry.require("workflow", original_plan.workflow_id)
    assert isinstance(original_workflow, WorkflowDefinition)
    saved_plan = WorkflowCompiler(executors).compile(original_workflow, registry)
    registry._definitions["tool"]["double"] = original_tool.model_copy(
        update={"implementation": "fixture:new-double"}
    )
    current_plan = WorkflowCompiler(executors).compile(original_workflow, registry)
    store = FileWorkflowStateStore(tmp_path)
    store.save_plan("saved-tool-run", saved_plan)

    def bind_saved_tool(definition: ToolDefinition, _contracts):
        implementations = {
            "fixture:old-double": lambda value: value * 2,
            "fixture:new-double": lambda value: value * 3,
        }
        return implementations[definition.implementation]

    completed = await WorkflowRuntimeHost(
        executors,
        store,
        InMemoryWorkflowEventSink(),
    ).execute(
        current_plan,
        WorkflowState.for_plan("saved-tool-run", saved_plan),
        RuntimeContext(
            tools={"double": lambda value: value * 3},
            contracts=contracts,
            definitions=registry,
            plan_tool_factory=bind_saved_tool,
        ),
    )

    assert completed.outputs == {"result": 8}


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
    assert [
        event.data["target"]
        for event in events.events
        if event.kind == "branch.selected"
    ] == ["repeat", "double", "repeat", "double", "finish"]
    assert events.events[-1].kind == "workflow.completed"


@pytest.mark.asyncio
async def test_runtime_host_reexecutes_completed_subworkflow_on_loop_reentry(
    tmp_path: Path,
) -> None:
    registry, executors, _plan, contracts = _workflow()
    child = WorkflowDefinition(
        id="loop-child",
        version="1.0.0",
        description="One reusable loop body",
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
                "id": "finish-child",
                "kind": "end_workflow",
                "output_variable": "doubled",
            },
        ],
    )
    parent = WorkflowDefinition(
        id="loop-parent",
        version="1.0.0",
        description="Loop over a completed child with the prior result as input",
        max_iterations=12,
        state={"value": 1},
        actions=[
            {
                "id": "call-child",
                "kind": "subworkflow",
                "workflow": child.id,
                "input_variable": "value",
                "child_input_variable": "input",
                "child_output_name": "result",
                "output_variable": "value",
            },
            {
                "id": "continue-loop",
                "kind": "if",
                "condition": {
                    "variable": "value",
                    "operator": "lt",
                    "value": 4,
                },
                "then": "repeat",
                "otherwise": "finish-parent",
            },
            {"id": "repeat", "kind": "goto", "target": "call-child"},
            {
                "id": "finish-parent",
                "kind": "end_workflow",
                "output_variable": "value",
            },
        ],
    )
    registry.register(child)
    registry.register(parent)
    compiler = WorkflowCompiler(executors)
    child_plan = compiler.compile(child, registry)
    parent_plan = compiler.compile(parent, registry)
    calls = 0

    def double(value: int) -> int:
        nonlocal calls
        calls += 1
        return value * 2

    completed = await WorkflowRuntimeHost(
        executors,
        FileWorkflowStateStore(tmp_path),
        InMemoryWorkflowEventSink(),
    ).execute(
        parent_plan,
        WorkflowState.for_plan("loop-subworkflow-run", parent_plan),
        RuntimeContext(
            tools={"double": double},
            contracts=contracts,
            definitions=registry,
            subworkflows={child.id: child_plan},
        ),
    )

    assert completed.status is WorkflowStatus.COMPLETED
    assert completed.outputs == {"result": 4}
    assert calls == 2
    assert completed.subworkflow_states["call-child"]["variables"] == {}
    assert completed.subworkflow_states["call-child"]["outputs"] == {"result": 4}


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
async def test_runtime_host_serializes_direct_parallel_tools_not_declared_safe(
    tmp_path: Path,
) -> None:
    registry, executors, _plan, contracts = _workflow()
    registry.register(
        ToolDefinition(
            id="unsafe-double",
            version="1.0.0",
            description="Double through an ordered stateful implementation",
            implementation="fixture:unsafe-double",
            input_contract="number",
            output_contract="number",
            side_effect="ordered_state",
            parallel_safe=False,
        )
    )
    workflow = WorkflowDefinition(
        id="host-unsafe-parallel",
        version="1.0.0",
        description="Serialize direct branches using an unsafe Tool",
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
                "tool": "unsafe-double",
                "input_variable": "left-input",
                "output_variable": "left-output",
            },
            {"id": "left-done", "kind": "goto", "target": "join"},
            {
                "id": "right",
                "kind": "invoke_tool",
                "tool": "unsafe-double",
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
    maximum_active = 0

    async def unsafe_double(value: int) -> int:
        nonlocal active, maximum_active
        active += 1
        maximum_active = max(maximum_active, active)
        await asyncio.sleep(0)
        active -= 1
        return value * 2

    completed = await WorkflowRuntimeHost(
        executors,
        FileWorkflowStateStore(tmp_path),
        InMemoryWorkflowEventSink(),
    ).execute(
        plan,
        WorkflowState.for_plan("unsafe-parallel-host-run", plan),
        RuntimeContext(
            tools={"unsafe-double": unsafe_double},
            contracts=contracts,
            definitions=registry,
        ),
    )

    assert maximum_active == 1
    assert completed.outputs == {"result": {"left": 2, "right": 4}}


@pytest.mark.asyncio
async def test_nested_parallel_persists_progress_and_retries_only_failed_branch(
    tmp_path: Path,
) -> None:
    registry, executors, _plan, contracts = _workflow()
    workflow = WorkflowDefinition(
        id="recoverable-parallel",
        version="1.0.0",
        description="Persist sibling progress across one branch failure",
        state={
            "left-input": 1,
            "right-input": 2,
            "retained-history": {"payload": "branch-history"},
        },
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
    parent = WorkflowDefinition(
        id="recoverable-parallel-parent",
        version="1.0.0",
        description="Parent retaining its child's parallel failure state",
        state={"child-input": {}},
        actions=[
            {
                "id": "call-child",
                "kind": "subworkflow",
                "workflow": workflow.id,
                "input_variable": "child-input",
                "child_input_variable": "unused",
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
    registry.register(workflow)
    registry.register(parent)
    plan = WorkflowCompiler(executors).compile(parent, registry)
    calls = {1: 0, 2: 0}

    async def flaky_double(value: int) -> int:
        calls[value] += 1
        await asyncio.sleep(0)
        if value == 2 and calls[value] == 1:
            raise RuntimeError("injected parallel failure")
        return value * 2

    store = FileWorkflowStateStore(tmp_path)
    host = WorkflowRuntimeHost(executors, store, InMemoryWorkflowEventSink())
    context = RuntimeContext(
        tools={"double": flaky_double},
        contracts=contracts,
        definitions=registry,
    )
    initial = WorkflowState.for_plan("recoverable-parallel-run", plan)
    child_initial = WorkflowState.for_plan(
        initial.run_id,
        plan.subworkflow_plans[workflow.id],
        initial_variables={"unused": {}},
    )
    child_initial.conversations = {
        "review": {"external_session_id": "session-before-parallel"}
    }
    initial.subworkflow_states["call-child"] = child_initial.model_dump(mode="json")

    with pytest.raises(RuntimeError, match="injected parallel failure"):
        await host.execute(plan, initial, context)

    failed = store.load("recoverable-parallel-run")
    child_failed = failed.subworkflow_states["call-child"]
    assert child_failed["parallel_states"]["parallel"]["left"]["status"] == (
        "completed"
    )
    assert child_failed["parallel_states"]["parallel"]["right"]["status"] == (
        "failed"
    )
    assert child_failed["parallel_states"]["parallel"]["left"]["variables"] == {
        "left-output": 2
    }
    assert child_failed["parallel_results"]["parallel"]["left"] == {
        "left-output": 2
    }
    assert child_failed["parallel_states"]["parallel"]["right"]["variables"][
        "retained-history"
    ] == {"payload": "branch-history"}
    assert child_failed["parallel_states"]["parallel"]["left"]["conversations"] == {
        "review": {"external_session_id": "session-before-parallel"}
    }
    assert child_failed["parallel_states"]["parallel"]["right"]["conversations"] == {
        "review": {"external_session_id": "session-before-parallel"}
    }

    completed = await host.execute(plan, failed, context)

    assert completed.outputs == {"result": {"left": 2, "right": 4}}
    assert completed.subworkflow_states["call-child"]["conversations"] == {
        "review": {"external_session_id": "session-before-parallel"}
    }
    assert calls == {1: 1, 2: 2}


@pytest.mark.asyncio
async def test_runtime_host_nests_subworkflow_state_in_the_parent_run(
    tmp_path: Path,
) -> None:
    registry, executors, _plan, contracts = _workflow()
    child = WorkflowDefinition(
        id="host-child",
        version="1.0.0",
        description="Child fixture",
        state={
            "input": 0,
            "retained-history": {"payload": "subworkflow-history"},
        },
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
    assert completed.subworkflow_states["call-child"]["variables"] == {}
    assert completed.subworkflow_states["call-child"]["actions"] == {}
    assert completed.subworkflow_states["call-child"]["outputs"] == {"result": 8}
    assert [
        (event.kind, event.workflow_id, event.action_id)
        for event in events.events
        if event.workflow_id == "host-child"
    ] == [
        ("workflow.started", "host-child", None),
        ("action.started", "host-child", "child-double"),
        ("tool.invoked", "host-child", "child-double"),
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
    assert completed.subworkflow_states["call-child"]["variables"] == {}
    assert completed.subworkflow_states["call-child"]["outputs"] == {"result": 8}


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
        interactions=["child-interaction"],
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
    assert completed.subworkflow_states["call-child"]["variables"] == {}
    assert completed.subworkflow_states["call-child"]["outputs"] == {
        "result": "Ada"
    }
    assert [
        event.action_id
        for event in events.events
        if event.kind == "action.started" and event.workflow_id == parent.id
    ].count("before-child") == 1


@pytest.mark.asyncio
async def test_saved_parent_plan_freezes_nested_workflow_for_resume(
    tmp_path: Path,
) -> None:
    registry, executors, _plan, contracts = _workflow()
    contracts.update(
        _register_text_interaction(
            registry,
            contract_id="frozen-child-answer",
            interaction_id="frozen-child-interaction",
        )
    )
    child = WorkflowDefinition(
        id="frozen-waiting-child",
        version="1.0.0",
        description="Child definition persisted with its parent plan",
        interactions=["frozen-child-interaction"],
        actions=[
            {
                "id": "ask-frozen-child",
                "kind": "request_input",
                "interaction": "frozen-child-interaction",
                "output_variable": "answer",
            },
            {
                "id": "finish-frozen-child",
                "kind": "end_workflow",
                "output_variable": "answer",
                "output_name": "result",
            },
        ],
    )
    parent = WorkflowDefinition(
        id="frozen-waiting-parent",
        version="1.0.0",
        description="Parent whose saved plan owns the child plan",
        state={"seed": "initial"},
        actions=[
            {
                "id": "call-frozen-child",
                "kind": "subworkflow",
                "workflow": child.id,
                "input_variable": "seed",
                "child_output_name": "result",
                "output_variable": "child-result",
            },
            {
                "id": "finish-frozen-parent",
                "kind": "end_workflow",
                "output_variable": "child-result",
            },
        ],
    )
    registry.register(child)
    registry.register(parent)
    compiler = WorkflowCompiler(executors)
    original_parent_plan = compiler.compile(parent, registry)
    store = FileWorkflowStateStore(tmp_path)
    host = WorkflowRuntimeHost(executors, store, InMemoryWorkflowEventSink())
    context = RuntimeContext(contracts=contracts, definitions=registry)

    waiting = await host.execute(
        original_parent_plan,
        WorkflowState.for_plan("frozen-subworkflow-run", original_parent_plan),
        context,
    )
    saved_parent_plan = store.load_plan("frozen-subworkflow-run")

    changed_child = child.model_copy(deep=True)
    changed_child.actions[-1]["output_name"] = "changed-result"
    registry._definitions["workflow"][child.id] = changed_child
    changed_parent_plan = compiler.compile(parent, registry)
    resumed = resume_waiting_input(
        saved_parent_plan,
        waiting,
        input_id="ask-frozen-child",
        values="Ada",
        contracts=contracts,
    )
    completed = await host.execute(changed_parent_plan, resumed, context)

    assert saved_parent_plan.subworkflow_plans[child.id].workflow_version == "1.0.0"
    assert completed.status is WorkflowStatus.COMPLETED
    assert completed.outputs == {"result": "Ada"}


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
        interactions=["branch-interaction"],
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
        state={
            "left-input": "left-seed",
            "right-input": 5,
            "retained-history": {"payload": "waiting-history"},
        },
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
    assert waiting.parallel_states["parallel"]["right"]["variables"] == {
        "right-output": 10
    }
    assert waiting.parallel_states["parallel"]["left"]["variables"][
        "retained-history"
    ] == {"payload": "waiting-history"}
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

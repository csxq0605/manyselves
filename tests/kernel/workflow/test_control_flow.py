import asyncio
from pathlib import Path

import pytest

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
from manyselves.kernel.workflow import CompilerError, WorkflowCompiler, WorkflowState
from manyselves.runtime.state_store import FileWorkflowStateStore


def _definitions() -> DefinitionRegistry:
    registry = DefinitionRegistry()
    number = ContractDefinition(
        id="number",
        version="1.0.0",
        description="A number",
        adapter="json_schema",
        schema={"type": "integer"},
    )
    registry.register(number)
    for tool_id in ("increment", "double"):
        registry.register(
            ToolDefinition(
                id=tool_id,
                version="1.0.0",
                description=tool_id,
                implementation=f"tests:{tool_id}",
                input_contract=number.id,
                output_contract=number.id,
                side_effect="pure_read",
                parallel_safe=True,
            )
        )
    return registry


def _compile(workflow: WorkflowDefinition, definitions=None):
    executors = build_builtin_executor_registry()
    registry = definitions or _definitions()
    return executors, WorkflowCompiler(executors).compile(workflow, registry)


@pytest.mark.asyncio
async def test_if_and_goto_execute_a_declared_finite_loop(tmp_path: Path) -> None:
    workflow = WorkflowDefinition(
        id="neutral-loop",
        version="1.0.0",
        description="Increment until three",
        state={"count": 0},
        max_iterations=10,
        actions=[
            {
                "id": "increment",
                "kind": "invoke_tool",
                "tool": "increment",
                "input_variable": "count",
                "output_variable": "count",
            },
            {
                "id": "check",
                "kind": "if",
                "condition": {"variable": "count", "operator": "lt", "value": 3},
                "then": "repeat",
                "otherwise": "finish",
            },
            {"id": "repeat", "kind": "goto", "target": "increment"},
            {
                "id": "finish",
                "kind": "end_workflow",
                "output_variable": "count",
            },
        ],
    )
    executors, plan = _compile(workflow)
    state = WorkflowState.for_plan("run-loop", plan)

    completed = await ControlFlowWorkflowExecutor(
        executors,
        FileWorkflowStateStore(tmp_path),
    ).execute(
        plan,
        state,
        RuntimeContext(tools={"increment": lambda value: value + 1}),
    )

    assert completed.outputs == {"result": 3}
    assert completed.control_steps == 9


@pytest.mark.asyncio
async def test_if_exit_condition_allows_a_loop_without_an_iteration_cap(
    tmp_path: Path,
) -> None:
    workflow = WorkflowDefinition(
        id="neutral-exit-loop",
        version="1.0.0",
        description="Increment until the declared condition exits",
        state={"count": 0},
        actions=[
            {
                "id": "increment",
                "kind": "invoke_tool",
                "tool": "increment",
                "input_variable": "count",
                "output_variable": "count",
            },
            {
                "id": "check",
                "kind": "if",
                "condition": {"variable": "count", "operator": "lt", "value": 3},
                "then": "repeat",
                "otherwise": "finish",
            },
            {"id": "repeat", "kind": "goto", "target": "increment"},
            {
                "id": "finish",
                "kind": "end_workflow",
                "output_variable": "count",
            },
        ],
    )
    executors, plan = _compile(workflow)

    completed = await ControlFlowWorkflowExecutor(
        executors,
        FileWorkflowStateStore(tmp_path),
    ).execute(
        plan,
        WorkflowState.for_plan("run-exit-loop", plan),
        RuntimeContext(tools={"increment": lambda value: value + 1}),
    )

    assert completed.outputs == {"result": 3}


@pytest.mark.asyncio
async def test_condition_group_exit_condition_allows_a_loop_without_an_iteration_cap(
    tmp_path: Path,
) -> None:
    workflow = WorkflowDefinition(
        id="neutral-condition-group-loop",
        version="1.0.0",
        description="Increment until the declared condition group exits",
        state={"count": 0},
        actions=[
            {
                "id": "increment",
                "kind": "invoke_tool",
                "tool": "increment",
                "input_variable": "count",
                "output_variable": "count",
            },
            {
                "id": "check",
                "kind": "condition_group",
                "branches": [
                    {
                        "condition": {
                            "variable": "count",
                            "operator": "lt",
                            "value": 3,
                        },
                        "target": "repeat",
                    }
                ],
                "default": "finish",
            },
            {"id": "repeat", "kind": "goto", "target": "increment"},
            {
                "id": "finish",
                "kind": "end_workflow",
                "output_variable": "count",
            },
        ],
    )
    executors, plan = _compile(workflow)

    completed = await ControlFlowWorkflowExecutor(
        executors,
        FileWorkflowStateStore(tmp_path),
    ).execute(
        plan,
        WorkflowState.for_plan("run-condition-group-loop", plan),
        RuntimeContext(tools={"increment": lambda value: value + 1}),
    )

    assert completed.outputs == {"result": 3}


@pytest.mark.asyncio
async def test_condition_group_uses_first_matching_branch(tmp_path: Path) -> None:
    workflow = WorkflowDefinition(
        id="neutral-condition-group",
        version="1.0.0",
        description="Choose one neutral value",
        state={"score": 7, "low": "low", "high": "high", "fallback": "fallback"},
        actions=[
            {
                "id": "choose",
                "kind": "condition_group",
                "branches": [
                    {
                        "condition": {"variable": "score", "operator": "gte", "value": 5},
                        "target": "use-high",
                    },
                    {
                        "condition": {"variable": "score", "operator": "gte", "value": 1},
                        "target": "use-low",
                    },
                ],
                "default": "use-fallback",
            },
            {"id": "use-high", "kind": "goto", "target": "finish-high"},
            {"id": "use-low", "kind": "goto", "target": "finish-low"},
            {"id": "use-fallback", "kind": "goto", "target": "finish-fallback"},
            {
                "id": "finish-high",
                "kind": "end_workflow",
                "output_variable": "high",
            },
            {
                "id": "finish-low",
                "kind": "end_workflow",
                "output_variable": "low",
            },
            {
                "id": "finish-fallback",
                "kind": "end_workflow",
                "output_variable": "fallback",
            },
        ],
    )
    executors, plan = _compile(workflow)

    completed = await ControlFlowWorkflowExecutor(
        executors,
        FileWorkflowStateStore(tmp_path),
    ).execute(
        plan,
        WorkflowState.for_plan("run-conditions", plan),
        RuntimeContext(),
    )

    assert completed.outputs == {"result": "high"}


@pytest.mark.asyncio
async def test_foreach_repeats_neutral_body_and_resumes_after_collection(
    tmp_path: Path,
) -> None:
    calls: list[int] = []

    def double(value: int) -> int:
        calls.append(value)
        return value * 2

    workflow = WorkflowDefinition(
        id="neutral-foreach",
        version="1.0.0",
        description="Double each item",
        state={"items": [1, 2, 3]},
        max_iterations=20,
        actions=[
            {
                "id": "each",
                "kind": "for_each",
                "items_variable": "items",
                "item_variable": "item",
                "body": "double",
                "after": "finish",
            },
            {
                "id": "double",
                "kind": "invoke_tool",
                "tool": "double",
                "input_variable": "item",
                "output_variable": "doubled",
            },
            {"id": "next", "kind": "goto", "target": "each"},
            {
                "id": "finish",
                "kind": "end_workflow",
                "output_variable": "doubled",
            },
        ],
    )
    executors, plan = _compile(workflow)

    completed = await ControlFlowWorkflowExecutor(
        executors,
        FileWorkflowStateStore(tmp_path),
    ).execute(
        plan,
        WorkflowState.for_plan("run-foreach", plan),
        RuntimeContext(tools={"double": double}),
    )

    assert calls == [1, 2, 3]
    assert completed.outputs == {"result": 6}
    assert completed.control_frames == {}


@pytest.mark.asyncio
async def test_loaded_foreach_state_continues_after_completed_iteration(
    tmp_path: Path,
) -> None:
    calls: list[int] = []
    workflow = WorkflowDefinition(
        id="resume-foreach",
        version="1.0.0",
        description="Resume a finite collection",
        state={"items": [1, 2, 3]},
        max_iterations=20,
        actions=[
            {
                "id": "each",
                "kind": "for_each",
                "items_variable": "items",
                "item_variable": "item",
                "body": "double",
                "after": "finish",
            },
            {
                "id": "double",
                "kind": "invoke_tool",
                "tool": "double",
                "input_variable": "item",
                "output_variable": "doubled",
            },
            {"id": "next", "kind": "goto", "target": "each"},
            {"id": "finish", "kind": "end_workflow", "output_variable": "doubled"},
        ],
    )
    executors, plan = _compile(workflow)
    store = FileWorkflowStateStore(tmp_path)
    partial = WorkflowState.for_plan("run-resume-foreach", plan)
    partial.variables.update({"item": 1, "doubled": 2})
    partial.control_frames["each"] = {"items": [1, 2, 3], "index": 0}
    partial.next_action_id = "next"
    partial.next_action_index = 2
    partial.control_steps = 2
    store.save(partial)

    completed = await ControlFlowWorkflowExecutor(executors, store).execute(
        plan,
        store.load(partial.run_id),
        RuntimeContext(
            tools={
                "double": lambda value: (calls.append(value), value * 2)[1],
            }
        ),
    )

    assert calls == [2, 3]
    assert completed.outputs == {"result": 6}


@pytest.mark.asyncio
async def test_parallel_branches_join_declared_outputs(tmp_path: Path) -> None:
    active = 0
    maximum_active = 0

    async def double(value: int) -> int:
        nonlocal active, maximum_active
        active += 1
        maximum_active = max(maximum_active, active)
        await asyncio.sleep(0)
        active -= 1
        return value * 2

    workflow = WorkflowDefinition(
        id="neutral-parallel",
        version="1.0.0",
        description="Run two neutral branches",
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
    executors, plan = _compile(workflow)

    completed = await ControlFlowWorkflowExecutor(
        executors,
        FileWorkflowStateStore(tmp_path),
    ).execute(
        plan,
        WorkflowState.for_plan("run-parallel", plan),
        RuntimeContext(tools={"double": double}),
    )

    assert maximum_active == 2
    assert completed.outputs == {"result": {"left": 2, "right": 4}}


@pytest.mark.asyncio
async def test_parallel_branches_honor_declared_concurrency_limit(
    tmp_path: Path,
) -> None:
    active = 0
    maximum_active = 0

    async def double(value: int) -> int:
        nonlocal active, maximum_active
        active += 1
        maximum_active = max(maximum_active, active)
        await asyncio.sleep(0.01)
        active -= 1
        return value * 2

    actions: list[dict] = [
        {
            "id": "parallel",
            "kind": "parallel",
            "branches": {
                "one": "one",
                "two": "two",
                "three": "three",
                "four": "four",
            },
            "join": "join",
            "max_concurrency": 2,
        }
    ]
    for branch in ("one", "two", "three", "four"):
        actions.extend(
            [
                {
                    "id": branch,
                    "kind": "invoke_tool",
                    "tool": "double",
                    "input_variable": f"{branch}-input",
                    "output_variable": f"{branch}-output",
                },
                {
                    "id": f"{branch}-done",
                    "kind": "goto",
                    "target": "join",
                },
            ]
        )
    actions.extend(
        [
            {
                "id": "join",
                "kind": "join",
                "parallel": "parallel",
                "inputs": {
                    branch: f"{branch}-output"
                    for branch in ("one", "two", "three", "four")
                },
                "output_variable": "joined",
            },
            {
                "id": "finish",
                "kind": "end_workflow",
                "output_variable": "joined",
            },
        ]
    )
    workflow = WorkflowDefinition(
        id="limited-parallel",
        version="1.0.0",
        description="Limit neutral branch concurrency",
        state={
            "one-input": 1,
            "two-input": 2,
            "three-input": 3,
            "four-input": 4,
        },
        actions=actions,
    )
    executors, plan = _compile(workflow)

    completed = await ControlFlowWorkflowExecutor(
        executors,
        FileWorkflowStateStore(tmp_path),
    ).execute(
        plan,
        WorkflowState.for_plan("run-limited-parallel", plan),
        RuntimeContext(tools={"double": double}),
    )

    assert maximum_active == 2
    assert completed.outputs["result"] == {
        "one": 2,
        "two": 4,
        "three": 6,
        "four": 8,
    }


@pytest.mark.asyncio
async def test_subworkflow_binds_parent_input_and_child_output(tmp_path: Path) -> None:
    child = WorkflowDefinition(
        id="double-child",
        version="1.0.0",
        description="Double one input",
        state={"input": 0},
        actions=[
            {
                "id": "double",
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
        id="parent",
        version="1.0.0",
        description="Call a child workflow",
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
    definitions = _definitions()
    definitions.register(child)
    definitions.register(parent)
    executors, child_plan = _compile(child, definitions)
    _executors, parent_plan = _compile(parent, definitions)

    completed = await ControlFlowWorkflowExecutor(
        executors,
        FileWorkflowStateStore(tmp_path),
    ).execute(
        parent_plan,
        WorkflowState.for_plan("run-parent", parent_plan),
        RuntimeContext(
            tools={"double": lambda value: value * 2},
            subworkflows={child.id: child_plan},
        ),
    )

    assert completed.outputs == {"result": 8}


def test_compiler_rejects_missing_target_and_unbounded_back_edge() -> None:
    missing = WorkflowDefinition(
        id="missing-target",
        version="1.0.0",
        description="Invalid target",
        state={"flag": True, "result": "done"},
        actions=[
            {
                "id": "choose",
                "kind": "if",
                "condition": {"variable": "flag", "operator": "truthy"},
                "then": "missing",
                "otherwise": "finish",
            },
            {"id": "finish", "kind": "end_workflow", "output_variable": "result"},
        ],
    )
    cycle = WorkflowDefinition(
        id="unbounded-cycle",
        version="1.0.0",
        description="Cycle without a declared limit",
        state={"result": "done"},
        actions=[
            {"id": "again", "kind": "goto", "target": "again"},
            {"id": "finish", "kind": "end_workflow", "output_variable": "result"},
        ],
    )

    with pytest.raises(CompilerError, match="missing action target"):
        _compile(missing)
    with pytest.raises(CompilerError, match="max_iterations"):
        _compile(cycle)


def test_compiler_rejects_loop_without_local_exit_even_with_an_external_if() -> None:
    workflow = WorkflowDefinition(
        id="external-if-unbounded-loop",
        version="1.0.0",
        description="An unrelated branch must not bless an unbounded loop",
        state={"flag": True, "result": "done"},
        actions=[
            {
                "id": "choose-entry",
                "kind": "if",
                "condition": {"variable": "flag", "operator": "truthy"},
                "then": "loop",
                "otherwise": "finish",
            },
            {"id": "loop", "kind": "goto", "target": "loop"},
            {"id": "finish", "kind": "end_workflow", "output_variable": "result"},
        ],
    )

    with pytest.raises(CompilerError, match="max_iterations"):
        _compile(workflow)

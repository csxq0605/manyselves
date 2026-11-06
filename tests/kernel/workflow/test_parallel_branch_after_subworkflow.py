"""Parallel concurrency checks the parent branch after a subworkflow call."""

from manyselves.kernel.definitions import (
    ContractDefinition,
    DefinitionRegistry,
    ToolDefinition,
    WorkflowDefinition,
)
from manyselves.kernel.executors import build_builtin_executor_registry
from manyselves.kernel.workflow import WorkflowCompiler


def _registry() -> DefinitionRegistry:
    registry = DefinitionRegistry()
    registry.register(
        ContractDefinition(
            id="number",
            version="1.0.0",
            description="A number",
            adapter="json_schema",
            schema={"type": "integer"},
        )
    )
    for tool_id, side_effect, parallel_safe in (
        ("read", "pure_read", True),
        ("write", "shared_write", False),
    ):
        registry.register(
            ToolDefinition(
                id=tool_id,
                version="1.0.0",
                description=tool_id,
                implementation=f"tests:{tool_id}",
                input_contract="number",
                output_contract="number",
                side_effect=side_effect,
                parallel_safe=parallel_safe,
            )
        )
    registry.register(
        WorkflowDefinition(
            id="child",
            version="1.0.0",
            description="Read one value",
            state={"input": 0},
            actions=[
                {
                    "id": "child-read",
                    "kind": "invoke_tool",
                    "tool": "read",
                    "input_variable": "input",
                    "output_variable": "read",
                },
                {
                    "id": "child-finish",
                    "kind": "end_workflow",
                    "output_variable": "read",
                },
            ],
        )
    )
    return registry


def _parent(tool_after_child: str) -> WorkflowDefinition:
    actions: list[dict] = [
        {
            "id": "parallel",
            "kind": "parallel",
            "branches": {"left": "left-child", "right": "right-read"},
            "join": "join",
            "max_concurrency": 2,
        },
        {
            "id": "left-child",
            "kind": "subworkflow",
            "workflow": "child",
            "input_variable": "left-input",
            "child_input_variable": "input",
            "child_output_name": "result",
            "output_variable": "left-child-output",
        },
        {
            "id": "left-after",
            "kind": "invoke_tool",
            "tool": tool_after_child,
            "input_variable": "left-child-output",
            "output_variable": "left-output",
        },
        {"id": "left-done", "kind": "goto", "target": "join"},
        {
            "id": "right-read",
            "kind": "invoke_tool",
            "tool": "read",
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
        {"id": "finish", "kind": "end_workflow", "output_variable": "joined"},
    ]
    return WorkflowDefinition(
        id="parent",
        version="1.0.0",
        description="Parallel branch that calls a child first",
        state={"left-input": 1, "right-input": 2},
        actions=actions,
    )


def _concurrency(tool_after_child: str) -> int:
    registry = _registry()
    parent = _parent(tool_after_child)
    registry.register(parent)
    plan = WorkflowCompiler(build_builtin_executor_registry()).compile(parent, registry)
    return plan.parallel_concurrency["parallel"]


def test_write_tool_after_subworkflow_runs_branches_one_at_a_time() -> None:
    assert _concurrency("write") == 1


def test_safe_read_tool_after_subworkflow_keeps_requested_concurrency() -> None:
    assert _concurrency("read") == 2

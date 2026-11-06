"""A workflow's model task may only be offered Tools that just read."""

from typing import Any, get_args

import pytest

from manyselves.kernel.definitions import (
    AgentDefinition,
    ContractDefinition,
    DefinitionRegistry,
    TaskDefinition,
    ToolDefinition,
    WorkflowDefinition,
)
from manyselves.kernel.executors import build_builtin_executor_registry
from manyselves.kernel.workflow import CompilerError, WorkflowCompiler

_WRITING_SIDE_EFFECTS = [
    side_effect
    for side_effect in get_args(ToolDefinition.model_fields["side_effect"].annotation)
    if side_effect != "pure_read"
]


def _registry(tool: ToolDefinition, *, task_tools: list[str]) -> DefinitionRegistry:
    registry = DefinitionRegistry()
    text = ContractDefinition(
        id="text",
        version="1.0.0",
        description="text",
        adapter="json_schema",
        schema={"type": "string"},
    )
    query = ContractDefinition(
        id="query",
        version="1.0.0",
        description="query",
        adapter="json_schema",
        schema={"type": "object"},
    )
    agent = AgentDefinition(
        id="writer",
        version="1.0.0",
        description="Writer",
        instructions="Write.",
        accepts=[text.id],
        produces=[text.id],
        tools=[tool.id],
    )
    task = TaskDefinition(
        id="write",
        version="1.0.0",
        description="Write text",
        agent=agent.id,
        objective="Write text",
        input_contract=text.id,
        output_contract=text.id,
        tools=task_tools,
    )
    for definition in (text, query, tool, agent, task):
        registry.register(definition)
    return registry


def _tool(**fields: Any) -> ToolDefinition:
    return ToolDefinition(
        id="workspace-tool",
        version="1.0.0",
        description="Workspace tool",
        implementation="fixture:workspace-tool",
        input_contract="query",
        output_contract="query",
        **fields,
    )


def _model_actions() -> list[dict[str, Any]]:
    return [
        {
            "id": "conversation",
            "kind": "create_conversation",
            "agent": "writer",
            "conversation_key": "writer",
            "output_variable": "conversation",
        },
        {
            "id": "draft",
            "kind": "invoke_agent",
            "agent": "writer",
            "task": "write",
            "conversation_variable": "conversation",
            "input_variable": "input",
            "output_variable": "result",
        },
        {"id": "finish", "kind": "end_workflow", "output_variable": "result"},
    ]


def _workflow(actions: list[dict[str, Any]], workflow_id: str = "drafting") -> WorkflowDefinition:
    return WorkflowDefinition(
        id=workflow_id,
        version="1.0.0",
        description="Model task with a Tool",
        tasks=["write"],
        state={"input": "hello", "query": {}},
        actions=actions,
    )


def _compile(workflow: WorkflowDefinition, registry: DefinitionRegistry):
    return WorkflowCompiler(build_builtin_executor_registry()).compile(workflow, registry)


def test_a_model_task_may_call_a_read_only_tool() -> None:
    registry = _registry(_tool(side_effect="pure_read"), task_tools=["workspace-tool"])

    plan = _compile(_workflow(_model_actions()), registry)

    assert plan.agent_tool_ids == ["workspace-tool"]


@pytest.mark.parametrize("side_effect", _WRITING_SIDE_EFFECTS)
def test_a_model_task_offered_a_tool_that_does_more_than_read_is_rejected(
    side_effect: str,
) -> None:
    registry = _registry(_tool(side_effect=side_effect), task_tools=["workspace-tool"])

    with pytest.raises(CompilerError) as raised:
        _compile(_workflow(_model_actions()), registry)

    assert str(raised.value) == (
        "workflow drafting action draft task write offers the model tool "
        f"workspace-tool with side_effect {side_effect}; a model task may only "
        "call pure_read tools"
    )


def test_a_tool_without_a_declared_side_effect_is_not_read_only() -> None:
    registry = _registry(_tool(), task_tools=["workspace-tool"])

    with pytest.raises(CompilerError, match="side_effect ordered_state"):
        _compile(_workflow(_model_actions()), registry)


def test_a_writing_tool_stays_available_to_the_workflow_steps() -> None:
    registry = _registry(_tool(side_effect="run_local_write"), task_tools=[])
    actions = _model_actions()
    actions.insert(
        2,
        {
            "id": "save",
            "kind": "invoke_tool",
            "tool": "workspace-tool",
            "input_variable": "query",
            "output_variable": "saved",
        },
    )

    plan = _compile(_workflow(actions), registry)

    assert plan.tool_ids == ["workspace-tool"]
    assert plan.agent_tool_ids == []


def test_a_writing_tool_in_a_child_workflow_names_the_child() -> None:
    registry = _registry(_tool(side_effect="shared_write"), task_tools=["workspace-tool"])
    child = _workflow(_model_actions(), workflow_id="child-drafting")
    registry.register(child)
    parent = WorkflowDefinition(
        id="parent",
        version="1.0.0",
        description="Call the drafting child",
        state={"input": "hello"},
        actions=[
            {
                "id": "call-child",
                "kind": "subworkflow",
                "workflow": child.id,
                "input_variable": "input",
                "output_variable": "child-result",
            },
            {"id": "finish", "kind": "end_workflow", "output_variable": "child-result"},
        ],
    )

    with pytest.raises(
        CompilerError,
        match="workflow child-drafting action draft task write offers the model tool "
        "workspace-tool with side_effect shared_write",
    ):
        _compile(parent, registry)

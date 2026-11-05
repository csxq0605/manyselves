"""Conversation roles are checked along the paths that reach each model call."""

from typing import Any

import pytest

from manyselves.kernel.definitions import (
    AgentDefinition,
    ContractDefinition,
    DefinitionRegistry,
    TaskDefinition,
    WorkflowDefinition,
)
from manyselves.kernel.executors import build_builtin_executor_registry
from manyselves.kernel.workflow import CompilerError, WorkflowCompiler


def _registry() -> DefinitionRegistry:
    registry = DefinitionRegistry()
    contract = ContractDefinition(
        id="text",
        version="1.0.0",
        description="text",
        adapter="json_schema",
        schema={"type": "string"},
    )
    registry.register(contract)
    for agent_id, task_id in (("writer", "write"), ("reviewer", "review")):
        registry.register(
            AgentDefinition(
                id=agent_id,
                version="1.0.0",
                description=agent_id,
                instructions="Return the input.",
                accepts=[contract.id],
                produces=[contract.id],
            )
        )
        registry.register(
            TaskDefinition(
                id=task_id,
                version="1.0.0",
                description=task_id,
                agent=agent_id,
                objective="Return text",
                input_contract=contract.id,
                output_contract=contract.id,
            )
        )
    return registry


def _conversation(action_id: str, agent: str, variable: str = "conversation") -> dict[str, Any]:
    return {
        "id": action_id,
        "kind": "create_conversation",
        "agent": agent,
        "conversation_key": f"{action_id}-session",
        "mode": "run",
        "output_variable": variable,
    }


def _invoke(
    action_id: str,
    agent: str,
    variable: str = "conversation",
    output: str = "result",
) -> dict[str, Any]:
    return {
        "id": action_id,
        "kind": "invoke_agent",
        "agent": agent,
        "task": {"writer": "write", "reviewer": "review"}[agent],
        "conversation_variable": variable,
        "input_variable": "input",
        "output_variable": output,
    }


def _workflow(
    actions: list[dict[str, Any]],
    *,
    state: dict[str, Any] | None = None,
    workflow_id: str = "roles",
    **extra: Any,
) -> WorkflowDefinition:
    return WorkflowDefinition(
        id=workflow_id,
        version="1.0.0",
        description="Conversation roles",
        tasks=["write", "review"],
        state={"input": "hello", "use-writer": True, **(state or {})},
        actions=actions,
        **extra,
    )


def _compile(workflow: WorkflowDefinition, registry: DefinitionRegistry | None = None):
    registry = registry or _registry()
    return WorkflowCompiler(build_builtin_executor_registry()).compile(workflow, registry)


def _branch(then: str, otherwise: str) -> dict[str, Any]:
    return {
        "id": "branch",
        "kind": "if",
        "condition": {"variable": "use-writer", "operator": "truthy"},
        "then": then,
        "otherwise": otherwise,
    }


def test_each_branch_may_use_a_conversation_of_its_own_role() -> None:
    plan = _compile(
        _workflow(
            [
                _branch("writer-conversation", "reviewer-conversation"),
                _conversation("writer-conversation", "writer"),
                _invoke("write", "writer"),
                {"id": "writer-done", "kind": "goto", "target": "finish"},
                _conversation("reviewer-conversation", "reviewer"),
                _invoke("review", "reviewer"),
                {"id": "finish", "kind": "end_workflow", "output_variable": "result"},
            ]
        )
    )

    assert plan.conversation_bindings["writer-conversation"]["agent"] == "writer"
    assert plan.conversation_bindings["reviewer-conversation"]["agent"] == "reviewer"


def test_branches_may_set_up_their_conversations_before_the_calls() -> None:
    # Both conversations are written before either call: a check in written
    # order would see the reviewer conversation at the writer call.
    _compile(
        _workflow(
            [
                _branch("writer-conversation", "reviewer-conversation"),
                _conversation("writer-conversation", "writer"),
                {"id": "to-write", "kind": "goto", "target": "write"},
                _conversation("reviewer-conversation", "reviewer"),
                {"id": "to-review", "kind": "goto", "target": "review"},
                _invoke("write", "writer"),
                {"id": "writer-done", "kind": "goto", "target": "finish"},
                _invoke("review", "reviewer"),
                {"id": "finish", "kind": "end_workflow", "output_variable": "result"},
            ]
        )
    )


def test_a_path_handing_one_roles_conversation_to_another_is_reported() -> None:
    # The reviewer conversation is written first, the writer one last: a check
    # in written order would see only the writer conversation at the call.
    workflow = _workflow(
        [
            _branch("keep-writer", "reviewer-conversation"),
            _conversation("reviewer-conversation", "reviewer"),
            {"id": "reviewer-done", "kind": "goto", "target": "write"},
            _conversation("keep-writer", "writer"),
            _invoke("write", "writer"),
            {"id": "finish", "kind": "end_workflow", "output_variable": "result"},
        ]
    )

    with pytest.raises(
        CompilerError,
        match="action write uses conversation for agent reviewer with agent writer",
    ):
        _compile(workflow)


def test_a_conversation_created_before_a_branch_reaches_both_sides() -> None:
    workflow = _workflow(
        [
            _conversation("conversation", "writer"),
            _branch("write", "review"),
            _invoke("write", "writer"),
            {"id": "writer-done", "kind": "goto", "target": "finish"},
            _invoke("review", "reviewer"),
            {"id": "finish", "kind": "end_workflow", "output_variable": "result"},
        ]
    )

    with pytest.raises(
        CompilerError,
        match="action review uses conversation for agent writer with agent reviewer",
    ):
        _compile(workflow)


def test_straight_line_cross_role_call_is_still_reported() -> None:
    with pytest.raises(
        CompilerError,
        match="action review uses conversation for agent writer with agent reviewer",
    ):
        _compile(
            _workflow(
                [
                    _conversation("conversation", "writer"),
                    _invoke("review", "reviewer"),
                    {"id": "finish", "kind": "end_workflow", "output_variable": "result"},
                ]
            )
        )


def test_a_later_conversation_replaces_the_role_on_a_straight_line() -> None:
    _compile(
        _workflow(
            [
                _conversation("first", "writer"),
                _invoke("write", "writer"),
                _conversation("second", "reviewer"),
                _invoke("review", "reviewer"),
                {"id": "finish", "kind": "end_workflow", "output_variable": "result"},
            ]
        )
    )


def test_a_fork_keeps_the_source_role() -> None:
    workflow = _workflow(
        [
            _conversation("conversation", "writer"),
            {
                "id": "fork",
                "kind": "fork_conversation",
                "source_variable": "conversation",
                "conversation_key": "fork-session",
                "output_variable": "forked",
            },
            _invoke("review", "reviewer", variable="forked"),
            {"id": "finish", "kind": "end_workflow", "output_variable": "result"},
        ]
    )

    with pytest.raises(
        CompilerError,
        match="action review uses conversation for agent writer with agent reviewer",
    ):
        _compile(workflow)


def test_a_copied_conversation_keeps_its_role() -> None:
    workflow = _workflow(
        [
            _conversation("conversation", "writer"),
            {
                "id": "copy",
                "kind": "set_variable",
                "variable": "copied",
                "value_variable": "conversation",
            },
            _invoke("review", "reviewer", variable="copied"),
            {"id": "finish", "kind": "end_workflow", "output_variable": "result"},
        ]
    )

    with pytest.raises(
        CompilerError,
        match="action review uses conversation for agent writer with agent reviewer",
    ):
        _compile(workflow)


def test_a_conversation_of_unknown_role_is_left_to_the_runtime_check() -> None:
    # A conversation taken from the starting state, merged with a writer one,
    # still names no other role than the caller's on any path.
    _compile(
        _workflow(
            [
                _branch("writer-conversation", "write"),
                _conversation("writer-conversation", "writer"),
                _invoke("write", "writer"),
                {"id": "finish", "kind": "end_workflow", "output_variable": "result"},
            ],
            state={"conversation": {"opaque": True}},
        )
    )
    _compile(
        _workflow(
            [
                _invoke("review", "reviewer"),
                {"id": "finish", "kind": "end_workflow", "output_variable": "result"},
            ],
            state={"conversation": {"opaque": True}},
        )
    )


def test_a_loop_body_keeps_the_role_created_before_the_loop() -> None:
    _compile(
        _workflow(
            [
                _conversation("conversation", "writer"),
                {
                    "id": "each",
                    "kind": "for_each",
                    "items_variable": "items",
                    "item_variable": "item",
                    "body": "write",
                    "after": "finish",
                },
                _invoke("write", "writer"),
                {"id": "next", "kind": "goto", "target": "each"},
                {"id": "finish", "kind": "end_workflow", "output_variable": "result"},
            ],
            state={"items": [1, 2], "result": ""},
        )
    )


def test_a_loop_back_edge_carrying_another_roles_conversation_is_reported() -> None:
    # The reviewer conversation is written after the writer call, but the
    # back edge brings it to the writer call on the second pass.
    workflow = _workflow(
        [
            _conversation("conversation", "writer"),
            {
                "id": "each",
                "kind": "for_each",
                "items_variable": "items",
                "item_variable": "item",
                "body": "write",
                "after": "finish",
            },
            _invoke("write", "writer"),
            _conversation("reviewer-conversation", "reviewer"),
            _invoke("review", "reviewer"),
            {"id": "next", "kind": "goto", "target": "each"},
            {"id": "finish", "kind": "end_workflow", "output_variable": "result"},
        ],
        state={"items": [1, 2], "result": ""},
    )

    with pytest.raises(
        CompilerError,
        match="action write uses conversation for agent reviewer with agent writer",
    ):
        _compile(workflow)


def _parallel(*, branch_creates_writer: bool) -> list[dict[str, Any]]:
    return [
        {
            "id": "parallel",
            "kind": "parallel",
            "branches": {"left": "left-conversation", "right": "right-conversation"},
            "join": "join",
        },
        _conversation(
            "left-conversation",
            "writer" if branch_creates_writer else "reviewer",
        ),
        _invoke(
            "left",
            "writer" if branch_creates_writer else "reviewer",
            output="left-output",
        ),
        {"id": "left-done", "kind": "goto", "target": "join"},
        _conversation("right-conversation", "reviewer"),
        _invoke("right", "reviewer", output="right-output"),
        {"id": "right-done", "kind": "goto", "target": "join"},
        {
            "id": "join",
            "kind": "join",
            "parallel": "parallel",
            "inputs": {"left": "left-output", "right": "right-output"},
            "output_variable": "joined",
        },
    ]


def test_parallel_branches_each_use_a_conversation_of_their_own_role() -> None:
    _compile(
        _workflow(
            [
                *_parallel(branch_creates_writer=True),
                {"id": "finish", "kind": "end_workflow", "output_variable": "joined"},
            ]
        )
    )


def test_a_branch_conversation_does_not_reach_past_the_join() -> None:
    # Branch variables stay in their branch: after the join the parent still
    # holds the writer conversation it created before the parallel action.
    _compile(
        _workflow(
            [
                _conversation("conversation", "writer"),
                *_parallel(branch_creates_writer=False),
                _invoke("write", "writer"),
                {"id": "finish", "kind": "end_workflow", "output_variable": "result"},
            ]
        )
    )


def test_a_branch_inherits_the_parent_conversation_role() -> None:
    workflow = _workflow(
        [
            _conversation("conversation", "writer"),
            {
                "id": "parallel",
                "kind": "parallel",
                "branches": {"left": "left", "right": "right"},
                "join": "join",
            },
            _invoke("left", "writer", output="left-output"),
            {"id": "left-done", "kind": "goto", "target": "join"},
            _invoke("right", "reviewer", output="right-output"),
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
    )

    with pytest.raises(
        CompilerError,
        match="action right uses conversation for agent writer with agent reviewer",
    ):
        _compile(workflow)


def test_a_conversation_passed_into_a_subworkflow_is_checked_when_it_runs() -> None:
    registry = _registry()
    child = WorkflowDefinition(
        id="child",
        version="1.0.0",
        description="Use the conversation the parent passes in",
        tasks=["review"],
        state={"input": "hello", "conversation": None},
        actions=[
            _invoke("review", "reviewer"),
            {"id": "child-finish", "kind": "end_workflow", "output_variable": "result"},
        ],
    )
    registry.register(child)
    parent = _workflow(
        [
            _conversation("conversation", "writer"),
            {
                "id": "call-child",
                "kind": "subworkflow",
                "workflow": child.id,
                "input_variables": {"conversation": "conversation", "input": "input"},
                "output_variable": "child-result",
            },
            {"id": "finish", "kind": "end_workflow", "output_variable": "child-result"},
        ]
    )
    registry.register(parent)

    plan = _compile(parent, registry)

    assert set(plan.subworkflow_plans) == {child.id}

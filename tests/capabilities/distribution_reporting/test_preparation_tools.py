"""Characterization for the file-defined Reporting preparation pipeline.

These tests describe the boundary that M6.5 is extracting from the legacy
``Workflow._prepare`` method.  Preparation is a Capability-owned sequence of
ordinary Tools; it must not invoke an Agent or a Provider, and its shared value
is a typed ``PreparationContext``.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

import pytest

from manyselves.capabilities.distribution_reporting import (
    load_distribution_reporting_capability,
)
from manyselves.kernel.definitions import DefinitionKind, ToolDefinition, WorkflowDefinition
from manyselves.kernel.executors import RuntimeContext, build_builtin_executor_registry
from manyselves.kernel.workflow import (
    ActionKind,
    ConditionGroupAction,
    IfAction,
    InvokeToolAction,
    WorkflowCompiler,
    WorkflowState,
    WorkflowStatus,
)
from manyselves.runtime.state_store import InMemoryWorkflowStateStore
from manyselves.runtime.workflow_host import InMemoryWorkflowEventSink, WorkflowRuntimeHost

PREPARATION_WORKFLOW_ID = "distribution-reporting-preparation"
PREPARATION_CONTEXT_MODULE = (
    "manyselves.capabilities.distribution_reporting.runtime.models.preparation"
)
PREPARATION_TOOLS_MODULE = (
    "manyselves.capabilities.distribution_reporting.runtime.preparation_tools"
)
PREPARATION_TOOL_IDS = (
    "build-manifest",
    "prepare-report-taxonomy",
    "parse-artifacts",
    "normalize-evidence",
    "evaluate-coverage",
    "load-special-topic-plan",
    "persist-preparation-snapshot",
    "finalize-preparation",
    "restore-preparation-snapshot",
)
NORMAL_PREPARATION_TOOL_IDS = PREPARATION_TOOL_IDS[:-1]


def _workflow(registry: Any) -> WorkflowDefinition:
    workflow = registry.require(DefinitionKind.WORKFLOW, PREPARATION_WORKFLOW_ID)
    assert isinstance(workflow, WorkflowDefinition)
    return workflow


def test_preparation_context_is_capability_owned_and_typed() -> None:
    preparation = import_module(PREPARATION_CONTEXT_MODULE)

    context_type = getattr(preparation, "PreparationContext")
    assert context_type.__module__ == PREPARATION_CONTEXT_MODULE

    # The context is a persisted carrier, not an untyped Reporting state dict.
    context = context_type.model_construct()
    assert isinstance(context, context_type)
    assert hasattr(context_type, "model_fields")


def test_file_definitions_expose_seven_fine_grained_preparation_tools() -> None:
    _capability, registry = load_distribution_reporting_capability()

    definitions = {
        definition.id: definition
        for definition in registry.all(DefinitionKind.TOOL)
        if isinstance(definition, ToolDefinition)
    }
    missing = sorted(set(PREPARATION_TOOL_IDS) - definitions.keys())
    assert not missing, f"M6.5 preparation Tool definitions are missing: {missing}"

    for tool_id in PREPARATION_TOOL_IDS:
        definition = definitions[tool_id]
        assert definition.implementation.startswith(
            "capability:distribution-reporting:"
        )
        assert definition.input_contract == "preparation_context"
        assert definition.output_contract == "preparation_context"
        assert definition.model_visible is False

    workflow = _workflow(registry)
    tool_actions = [
        action
        for action in workflow.actions
        if action.get("kind") == ActionKind.INVOKE_TOOL
    ]
    referenced_tools = [action["tool"] for action in tool_actions]
    assert set(PREPARATION_TOOL_IDS) <= set(referenced_tools)
    assert _ordered_subsequence(NORMAL_PREPARATION_TOOL_IDS, referenced_tools)
    assert "restore-preparation-snapshot" in referenced_tools
    assert not any(action.get("kind") == ActionKind.INVOKE_AGENT for action in workflow.actions)


def test_preparation_workflow_compiles_to_tool_only_plan() -> None:
    _capability, registry = load_distribution_reporting_capability()
    workflow = _workflow(registry)

    plan = WorkflowCompiler(build_builtin_executor_registry()).compile(
        workflow,
        registry,
    )

    assert plan.workflow_id == PREPARATION_WORKFLOW_ID
    assert set(PREPARATION_TOOL_IDS) <= set(plan.tool_ids)
    assert plan.agent_ids == []
    assert plan.task_ids == []
    assert all(action.kind is not ActionKind.INVOKE_AGENT for action in plan.actions)

    invoke_tools = [
        action.tool
        for action in plan.actions
        if isinstance(action, InvokeToolAction)
    ]
    assert _ordered_subsequence(NORMAL_PREPARATION_TOOL_IDS, invoke_tools)
    assert "restore-preparation-snapshot" in invoke_tools


@pytest.mark.asyncio
async def test_preparation_workflow_emits_ordered_tool_events_without_agent_or_provider(
) -> None:
    """The normal path is executable through the generic Host as Tool effects only."""

    preparation = import_module(PREPARATION_CONTEXT_MODULE)
    context_type = getattr(preparation, "PreparationContext")
    context_value = context_type.model_validate(
        {
            "run_id": "preparation-characterization",
            "request": {
                "operation": "full_report",
                "instruction": "Characterize deterministic preparation",
            },
        }
    )

    _capability, registry = load_distribution_reporting_capability()
    workflow = _workflow(registry)
    plan = WorkflowCompiler(build_builtin_executor_registry()).compile(
        workflow,
        registry,
    )

    calls: list[str] = []
    provider_calls: list[str] = []

    def tool(tool_id: str):
        def invoke(_arguments: Any) -> Any:
            calls.append(tool_id)
            return context_value

        return invoke

    tools = {tool_id: tool(tool_id) for tool_id in PREPARATION_TOOL_IDS}
    initial_variables = dict(plan.initial_state)
    for action in plan.actions:
        if isinstance(action, InvokeToolAction):
            if action.input_variable is not None:
                initial_variables.setdefault(action.input_variable, context_value)
            for variable in action.input_variables.values():
                initial_variables.setdefault(variable, context_value)
        elif isinstance(action, IfAction):
            initial_variables.setdefault(action.condition.variable, False)
        elif isinstance(action, ConditionGroupAction):
            for branch in action.branches:
                initial_variables.setdefault(branch.condition.variable, False)

    # The test deliberately uses an in-memory Store.  No Provider, agent,
    # filesystem, lock, CAS or additional admission path is involved.
    events = InMemoryWorkflowEventSink()
    completed = await WorkflowRuntimeHost(
        build_builtin_executor_registry(),
        InMemoryWorkflowStateStore(),
        events,
    ).execute(
        plan,
        WorkflowState.for_plan(
            "preparation-characterization",
            plan,
            initial_variables=initial_variables,
        ),
        RuntimeContext(
            tools=tools,
            definitions=registry,
            agents={
                "provider-spy": lambda *_args, **_kwargs: provider_calls.append(
                    "agent"
                )
            },
        ),
    )

    assert completed.status is WorkflowStatus.COMPLETED
    assert calls == list(NORMAL_PREPARATION_TOOL_IDS)
    assert provider_calls == []
    assert plan.agent_ids == []
    assert isinstance(completed.outputs["result"], context_type)
    assert [
        event.data["tool_id"]
        for event in events.events
        if event.kind == "tool.invoked"
    ] == list(NORMAL_PREPARATION_TOOL_IDS)


def test_top_level_file_workflow_prepares_before_module_cohort() -> None:
    _capability, registry = load_distribution_reporting_capability()
    top = registry.require(DefinitionKind.WORKFLOW, "distribution-reporting")
    assert isinstance(top, WorkflowDefinition)

    actions = top.actions
    preparation_index = next(
        index
        for index, action in enumerate(actions)
        if action.get("kind") == ActionKind.SUBWORKFLOW
        and action.get("workflow") == PREPARATION_WORKFLOW_ID
    )
    module_cohort_index = next(
        index
        for index, action in enumerate(actions)
        if action.get("kind") == ActionKind.SUBWORKFLOW
        and action.get("workflow") == "distribution-module-cohort"
    )
    assert preparation_index < module_cohort_index


def _ordered_subsequence(expected: tuple[str, ...], actual: list[str]) -> bool:
    positions = iter(actual)
    return all(any(candidate == item for candidate in positions) for item in expected)

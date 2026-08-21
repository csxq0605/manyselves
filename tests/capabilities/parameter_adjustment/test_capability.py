from pathlib import Path
from typing import Any

import pytest

from manyselves.kernel.contracts import build_contract_adapter
from manyselves.kernel.definitions import (
    ContractDefinition,
    DefinitionKind,
    WorkflowDefinition,
    load_capability,
)
from manyselves.kernel.executors import (
    ControlFlowWorkflowExecutor,
    RuntimeContext,
    build_builtin_executor_registry,
)
from manyselves.kernel.ports import AgentInvocationOutcome
from manyselves.kernel.workflow import WorkflowCompiler, WorkflowState
from manyselves.runtime.state_store import FileWorkflowStateStore

FIXTURE = (
    Path(__file__).parents[2]
    / "fixtures"
    / "capabilities"
    / "parameter_adjustment"
    / "capability.yaml"
)


class _ParameterAdjuster:
    def __init__(self) -> None:
        self.calls = 0

    async def invoke(self, *_args: Any, **_kwargs: Any) -> AgentInvocationOutcome:
        self.calls += 1
        return AgentInvocationOutcome(result={"value": 10})


def test_second_capability_is_neutral_and_loads_its_complete_definition_graph() -> None:
    capability, registry = load_capability(FIXTURE)

    assert capability.id == "parameter-adjustment"
    assert {definition.id for definition in registry.all(DefinitionKind.AGENT)} == {
        "parameter-adjuster"
    }
    assert {definition.id for definition in registry.all(DefinitionKind.WORKFLOW)} == {
        "parameter-adjustment"
    }
    serialized = "\n".join(
        definition.model_dump_json() for definition in registry.all()
    ).casefold()
    for reporting_term in (
        "reporting",
        "editor",
        "auditor",
        "cross",
        "chief",
        "module-2.",
    ):
        assert reporting_term not in serialized


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("value", "expected_agent_calls"),
    [(12, 0), (4, 1)],
)
async def test_second_capability_executes_tool_contract_condition_agent_and_goto(
    tmp_path: Path,
    value: int,
    expected_agent_calls: int,
) -> None:
    _capability, registry = load_capability(FIXTURE)
    workflow = registry.require(DefinitionKind.WORKFLOW, "parameter-adjustment")
    assert isinstance(workflow, WorkflowDefinition)
    workflow = workflow.model_copy(deep=True)
    workflow.state = {"parameters": {"value": value}}
    contracts = {
        definition.id: build_contract_adapter(definition)
        for definition in registry.all(DefinitionKind.CONTRACT)
        if isinstance(definition, ContractDefinition)
    }
    executors = build_builtin_executor_registry()
    plan = WorkflowCompiler(executors).compile(workflow, registry)
    store = FileWorkflowStateStore(tmp_path)
    adjuster = _ParameterAdjuster()

    state = await ControlFlowWorkflowExecutor(executors, store).execute(
        plan,
        WorkflowState.for_plan(f"parameter-{value}", plan),
        RuntimeContext(
            tools={"normalize-parameter": lambda values: values["value"]},
            contracts=contracts,
            agents={"parameter-adjuster": adjuster},
            definitions=registry,
        ),
    )

    assert state.outputs == {"result": max(value, 10)}
    assert adjuster.calls == expected_agent_calls
    assert state.status == "completed"

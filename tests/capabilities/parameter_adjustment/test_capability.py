from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from manyselves.capabilities.parameter_adjustment import CAPABILITY_FILE
from manyselves.capabilities.parameter_adjustment.adapters.runtime import (
    ParameterAdjustmentRuntimeBinding,
)
from manyselves.kernel.contracts import build_contract_adapter
from manyselves.kernel.definitions import (
    ContractDefinition,
    DefinitionKind,
    WorkflowDefinition,
    load_capability,
)
from manyselves.kernel.executors import RuntimeContext, build_builtin_executor_registry
from manyselves.kernel.ports import AgentInvocationOutcome
from manyselves.kernel.workflow import WorkflowCompiler, WorkflowState
from manyselves.runtime.state_store import FileWorkflowStateStore
from manyselves.runtime.workflow_host import (
    InMemoryWorkflowEventSink,
    WorkflowRuntimeHost,
)

FIXTURE = CAPABILITY_FILE


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

    state = await WorkflowRuntimeHost(
        executors,
        store,
        InMemoryWorkflowEventSink(),
    ).execute(
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


@pytest.mark.asyncio
async def test_production_binding_projects_generic_run_value_and_cost(
    tmp_path: Path,
) -> None:
    binding = ParameterAdjustmentRuntimeBinding(tmp_path)

    accepted = await binding.start(
        UUID("40000000-0000-4000-8000-000000000001"),
        "parameter-adjustment",
        {"value": 4},
    )
    run = binding.get_run(accepted["run_id"])
    outputs = binding.get_outputs(accepted["run_id"])
    cost = binding.get_cost(accepted["run_id"])

    assert accepted == {
        "run_id": "parameter-adjustment-40000000000040008000000000000001",
        "task_id": None,
    }
    assert run["run"]["status"] == "completed"
    assert run["run"]["capability_id"] == "parameter-adjustment"
    assert outputs == {
        "run_id": accepted["run_id"],
        "outputs": [{"id": "result", "kind": "value", "value": 10}],
    }
    assert cost["usage"]["totals"]["provider_attempts"] == 0
    assert cost["usage"]["totals"]["total_tokens"] == 0

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
    DefinitionRegistry,
    InteractionDefinition,
    ToolDefinition,
    WorkflowDefinition,
    load_capability,
)
from manyselves.kernel.executors import RuntimeContext, build_builtin_executor_registry
from manyselves.kernel.ports import AgentInvocationOutcome
from manyselves.kernel.workflow import WorkflowCompiler, WorkflowState, WorkflowStatus
from manyselves.runtime.capability_binding import CapabilityRunNotFoundError
from manyselves.runtime.state_store import FileWorkflowStateStore
from manyselves.runtime.tool_adapter import ToolAdapterError
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
        WorkflowState.for_plan(
            f"parameter-{value}",
            plan,
            initial_variables={"parameters": {"value": value}},
        ),
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


def test_parameter_input_binding_is_fully_declared_in_the_resolved_plan() -> None:
    _capability, registry = load_capability(FIXTURE)
    workflow = registry.require(DefinitionKind.WORKFLOW, "parameter-adjustment")
    assert isinstance(workflow, WorkflowDefinition)

    plan = WorkflowCompiler(build_builtin_executor_registry()).compile(
        workflow,
        registry,
    )

    assert workflow.state == {}
    assert workflow.input_variable == "parameters"
    assert plan.initial_state == {}
    assert plan.input_variable == "parameters"
    assert plan.input_contract == "parameter-input"
    assert "parameter-input" in plan.contract_ids


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


@pytest.mark.asyncio
async def test_provide_input_restores_definitions_and_tools_from_the_saved_plan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _capability, loaded = load_capability(FIXTURE)
    registry = DefinitionRegistry()
    for definition in loaded.all():
        if not isinstance(definition, WorkflowDefinition):
            registry.register(definition)
    interaction = InteractionDefinition(
        id="parameter-request",
        version="1.0.0",
        description="Request one parameter object",
        input_contract="parameter-input",
        title="Parameter",
    )
    workflow = WorkflowDefinition(
        id="parameter-adjustment",
        version="1.0.0",
        description="Resume one saved parameter request",
        interactions=[interaction.id],
        output_contract="parameter-value",
        actions=[
            {
                "id": "ask-parameter",
                "kind": "request_input",
                "interaction": interaction.id,
                "output_variable": "parameters",
            },
            {
                "id": "normalize",
                "kind": "invoke_tool",
                "tool": "normalize-parameter",
                "input_variable": "parameters",
                "output_variable": "normalized",
            },
            {
                "id": "finish",
                "kind": "end_workflow",
                "output_variable": "normalized",
            },
        ],
    )
    registry.register(interaction)
    registry.register(workflow)
    binding = ParameterAdjustmentRuntimeBinding(tmp_path)
    contracts = binding._contracts(registry)
    tools = binding._tools(registry, contracts)
    plan = WorkflowCompiler(build_builtin_executor_registry()).compile(
        workflow,
        registry,
    )
    run_id = "parameter-adjustment-saved-input"

    waiting = await binding._execute(
        plan,
        WorkflowState.for_plan(run_id, plan),
        registry,
        contracts,
        tools,
    )
    monkeypatch.setattr(
        "manyselves.capabilities.parameter_adjustment.adapters.runtime."
        "load_parameter_adjustment_capability",
        lambda: (_ for _ in ()).throw(AssertionError("read current definitions")),
    )

    resumed = await binding.provide_input(
        UUID("40000000-0000-4000-8000-000000000002"),
        run_id,
        input_id="ask-parameter",
        values={"value": 7},
    )

    assert waiting.status is WorkflowStatus.WAITING
    assert resumed == {"run_id": run_id, "task_id": None}
    assert binding.get_outputs(run_id)["outputs"] == [
        {"id": "result", "kind": "value", "value": 7}
    ]


def test_generic_waiting_run_is_inactive_so_the_ui_can_request_input(
    tmp_path: Path,
) -> None:
    run_id = "parameter-adjustment-waiting"
    FileWorkflowStateStore(tmp_path).save(
        WorkflowState(
            run_id=run_id,
            workflow_id="parameter-adjustment",
            status=WorkflowStatus.WAITING,
            waiting_input={"input_id": "number", "schema": {"type": "integer"}},
        )
    )

    projected = ParameterAdjustmentRuntimeBinding(tmp_path).get_run(run_id)

    assert projected["run"]["active"] is False
    assert projected["waiting_input"] == [
        {"input_id": "number", "schema": {"type": "integer"}}
    ]


def test_binding_does_not_claim_another_workflow_runtime_state(tmp_path: Path) -> None:
    run_id = "foreign-run"
    FileWorkflowStateStore(tmp_path).save(
        WorkflowState(run_id=run_id, workflow_id="another-capability")
    )

    with pytest.raises(CapabilityRunNotFoundError):
        ParameterAdjustmentRuntimeBinding(tmp_path).get_run(run_id)


@pytest.mark.asyncio
async def test_production_binding_rejects_an_unresolved_declared_tool_before_run_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    capability, loaded = load_capability(FIXTURE)
    registry = DefinitionRegistry()
    for definition in loaded.all():
        if isinstance(definition, ToolDefinition):
            definition = definition.model_copy(
                update={
                    "implementation": (
                        "capability:parameter-adjustment:missing-implementation"
                    )
                }
            )
        registry.register(definition)
    monkeypatch.setattr(
        "manyselves.capabilities.parameter_adjustment.adapters.runtime."
        "load_parameter_adjustment_capability",
        lambda: (capability, registry),
    )

    with pytest.raises(
        ToolAdapterError,
        match="parameter-adjustment:missing-implementation",
    ):
        await ParameterAdjustmentRuntimeBinding(tmp_path).start(
            UUID("40000000-0000-4000-8000-000000000099"),
            "parameter-adjustment",
            {"value": 4},
        )

    assert not (tmp_path / "Work/runs").exists()

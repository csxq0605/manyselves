from pathlib import Path

import pytest

from manyselves.kernel.contracts import build_contract_adapter
from manyselves.kernel.definitions import (
    ContractDefinition,
    DefinitionRegistry,
    ToolDefinition,
    WorkflowDefinition,
)
from manyselves.kernel.executors import (
    RuntimeContext,
    SequentialWorkflowExecutor,
    build_builtin_executor_registry,
)
from manyselves.kernel.workflow import (
    ActionExecutionStatus,
    CompilerError,
    EndWorkflowAction,
    InvokeToolAction,
    SetVariableAction,
    ValidateContractAction,
    WorkflowCompiler,
    WorkflowState,
    WorkflowStatus,
)
from manyselves.runtime.state_store import FileWorkflowStateStore


def _definitions() -> tuple[DefinitionRegistry, ContractDefinition]:
    contract = ContractDefinition(
        id="number-record",
        version="1.0.0",
        description="A neutral number record",
        adapter="json_schema",
        schema={
            "type": "object",
            "properties": {"value": {"type": "integer"}},
            "required": ["value"],
            "additionalProperties": False,
        },
    )
    tool = ToolDefinition(
        id="increment",
        version="1.0.0",
        description="Adds one",
        implementation="tests.kernel.workflow:increment",
        input_contract=contract.id,
        output_contract=contract.id,
        side_effect="pure_read",
        parallel_safe=True,
        reuse_result=True,
    )
    registry = DefinitionRegistry()
    registry.register(contract)
    registry.register(tool)
    return registry, contract


def _workflow() -> WorkflowDefinition:
    return WorkflowDefinition(
        id="neutral-increment",
        version="1.0.0",
        description="Increment and validate a neutral value",
        output_contract="number-record",
        actions=[
            {
                "id": "set-input",
                "kind": "set_variable",
                "variable": "input",
                "value": {"value": 1},
            },
            {
                "id": "invoke-increment",
                "kind": "invoke_tool",
                "tool": "increment",
                "input_variable": "input",
                "output_variable": "incremented",
            },
            {
                "id": "validate-output",
                "kind": "validate_contract",
                "contract": "number-record",
                "input_variable": "incremented",
                "output_variable": "validated",
            },
            {
                "id": "finish",
                "kind": "end_workflow",
                "output_variable": "validated",
                "output_name": "result",
            },
        ],
    )


def test_minimal_compiler_resolves_registered_sequential_actions() -> None:
    definitions, _contract = _definitions()
    executors = build_builtin_executor_registry()

    plan = WorkflowCompiler(executors).compile(_workflow(), definitions)

    assert [type(action) for action in plan.actions] == [
        SetVariableAction,
        InvokeToolAction,
        ValidateContractAction,
        EndWorkflowAction,
    ]
    assert plan.tool_ids == ["increment"]
    assert plan.contract_ids == ["number-record"]
    assert plan.final_output_contract == "number-record"


def test_minimal_compiler_rejects_read_before_definition() -> None:
    definitions, _contract = _definitions()
    workflow = _workflow().model_copy(deep=True)
    workflow.actions.pop(0)

    with pytest.raises(CompilerError, match="undefined variable: input"):
        WorkflowCompiler(build_builtin_executor_registry()).compile(
            workflow,
            definitions,
        )


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda workflow: workflow.actions.__setitem__(
                1,
                {**workflow.actions[1], "id": "set-input"},
            ),
            "duplicate action id: set-input",
        ),
        (
            lambda workflow: workflow.actions.__setitem__(
                1,
                {**workflow.actions[1], "tool": "missing-tool"},
            ),
            "references missing tool:missing-tool",
        ),
        (
            lambda workflow: workflow.actions.pop(),
            "requires one final end_workflow action",
        ),
    ],
)
def test_minimal_compiler_reports_required_plan_boundaries(
    mutate,
    message: str,
) -> None:
    definitions, _contract = _definitions()
    workflow = _workflow().model_copy(deep=True)
    mutate(workflow)

    with pytest.raises(CompilerError, match=message):
        WorkflowCompiler(build_builtin_executor_registry()).compile(
            workflow,
            definitions,
        )


@pytest.mark.asyncio
async def test_neutral_workflow_executes_and_persists_final_state(
    tmp_path: Path,
) -> None:
    definitions, contract = _definitions()
    executors = build_builtin_executor_registry()
    plan = WorkflowCompiler(executors).compile(_workflow(), definitions)
    calls: list[dict[str, int]] = []

    def increment(value: dict[str, int]) -> dict[str, int]:
        calls.append(value)
        return {"value": value["value"] + 1}

    context = RuntimeContext(
        tools={"increment": increment},
        contracts={contract.id: build_contract_adapter(contract)},
    )
    store = FileWorkflowStateStore(tmp_path)
    state = WorkflowState.for_plan("run-neutral", plan)
    store.save_plan(state.run_id, plan)

    completed = await SequentialWorkflowExecutor(executors, store).execute(
        plan,
        state,
        context,
    )

    assert completed.status is WorkflowStatus.COMPLETED
    assert completed.outputs == {"result": {"value": 2}}
    assert calls == [{"value": 1}]
    assert store.load("run-neutral") == completed
    assert store.load_plan("run-neutral") == plan


@pytest.mark.asyncio
async def test_tool_action_can_compose_declared_named_state_inputs(
    tmp_path: Path,
) -> None:
    definitions, contract = _definitions()
    workflow = WorkflowDefinition(
        id="neutral-compose",
        version="1.0.0",
        description="Compose two declared state values for one tool call",
        actions=[
            {
                "id": "set-left",
                "kind": "set_variable",
                "variable": "left",
                "value": 2,
            },
            {
                "id": "set-right",
                "kind": "set_variable",
                "variable": "right",
                "value": 3,
            },
            {
                "id": "compose",
                "kind": "invoke_tool",
                "tool": "increment",
                "input_variables": {"left": "left", "right": "right"},
                "output_variable": "composed",
            },
            {
                "id": "finish",
                "kind": "end_workflow",
                "output_variable": "composed",
            },
        ],
    )
    executors = build_builtin_executor_registry()
    plan = WorkflowCompiler(executors).compile(workflow, definitions)
    received: list[dict[str, int]] = []

    def compose(value: dict[str, int]) -> dict[str, int]:
        received.append(value)
        return {"value": value["left"] + value["right"]}

    completed = await SequentialWorkflowExecutor(
        executors,
        FileWorkflowStateStore(tmp_path),
    ).execute(
        plan,
        WorkflowState.for_plan("run-compose", plan),
        RuntimeContext(
            tools={"increment": compose},
            contracts={contract.id: build_contract_adapter(contract)},
        ),
    )

    assert received == [{"left": 2, "right": 3}]
    assert completed.outputs == {"result": {"value": 5}}


def test_tool_action_rejects_ambiguous_single_and_named_inputs() -> None:
    definitions, _contract = _definitions()
    workflow = _workflow().model_copy(deep=True)
    workflow.actions[1]["input_variables"] = {"value": "input"}

    with pytest.raises(CompilerError, match="exactly one input binding"):
        WorkflowCompiler(build_builtin_executor_registry()).compile(
            workflow,
            definitions,
        )


@pytest.mark.asyncio
async def test_loaded_partial_state_resumes_after_completed_action(
    tmp_path: Path,
) -> None:
    definitions, contract = _definitions()
    executors = build_builtin_executor_registry()
    plan = WorkflowCompiler(executors).compile(_workflow(), definitions)
    store = FileWorkflowStateStore(tmp_path)
    partial = WorkflowState.for_plan("run-resume", plan)
    partial.variables["input"] = {"value": 1}
    partial.actions["set-input"].status = ActionExecutionStatus.COMPLETED
    partial.next_action_index = 1
    store.save(partial)
    calls = 0

    def increment(value: dict[str, int]) -> dict[str, int]:
        nonlocal calls
        calls += 1
        return {"value": value["value"] + 1}

    context = RuntimeContext(
        tools={"increment": increment},
        contracts={contract.id: build_contract_adapter(contract)},
    )

    completed = await SequentialWorkflowExecutor(executors, store).execute(
        plan,
        store.load("run-resume"),
        context,
    )

    assert completed.outputs == {"result": {"value": 2}}
    assert calls == 1
    assert completed.actions["set-input"].status is ActionExecutionStatus.COMPLETED

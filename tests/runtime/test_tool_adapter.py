from pathlib import Path

import pytest

from manyselves.kernel.contracts import (
    ContractValidationError,
    build_contract_adapter,
)
from manyselves.kernel.definitions import (
    ContractDefinition,
    DefinitionRegistry,
    ToolDefinition,
    WorkflowDefinition,
)
from manyselves.kernel.executors import (
    RuntimeContext,
    build_builtin_executor_registry,
)
from manyselves.kernel.ports import ToolInvocationOutcome
from manyselves.kernel.workflow import WorkflowCompiler, WorkflowState
from manyselves.runtime import tool_adapter as tool_adapter_module
from manyselves.runtime.state_store import FileWorkflowStateStore
from manyselves.runtime.tool_adapter import (
    CapabilityToolAdapter,
    CapabilityToolAdapterFactory,
    ToolAdapter,
    ToolAdapterError,
)
from manyselves.runtime.tools import ReadTool, RunToolResultIndex, Tool
from manyselves.runtime.workflow_host import (
    InMemoryWorkflowEventSink,
    WorkflowRuntimeHost,
)


def _runtime(executors, store) -> WorkflowRuntimeHost:
    return WorkflowRuntimeHost(executors, store, InMemoryWorkflowEventSink())


def test_capability_tool_adapter_has_no_legacy_runtime_base() -> None:
    assert issubclass(CapabilityToolAdapter, ToolAdapter)
    assert not hasattr(tool_adapter_module, "LegacyToolAdapter")
    assert not hasattr(tool_adapter_module, "LegacyToolAdapterFactory")


def _contract(contract_id: str) -> ContractDefinition:
    return ContractDefinition(
        id=contract_id,
        version="1.0.0",
        description=f"Contract {contract_id}",
        adapter="json_schema",
        schema={
            "type": "object",
            "properties": {"value": {"type": "integer"}},
            "required": ["value"],
            "additionalProperties": False,
        },
    )


def _definition(*, reuse_result: bool = True) -> ToolDefinition:
    return ToolDefinition(
        id="increment",
        version="1.0.0",
        description="Adds one",
        implementation="capability:test-runtime:increment",
        input_contract="number-input",
        output_contract="number-output",
        side_effect="pure_read",
        parallel_safe=True,
        reuse_result=reuse_result,
    )


def _capability_definition(*, implementation: str) -> ToolDefinition:
    return ToolDefinition(
        id="normalize-parameter",
        version="1.0.0",
        description="Normalize one parameter",
        implementation=implementation,
        input_contract="parameter-input",
        output_contract="parameter-value",
        side_effect="pure_read",
        parallel_safe=True,
    )


def _capability_contracts() -> dict[str, object]:
    input_definition = ContractDefinition(
        id="parameter-input",
        version="1.0.0",
        description="One parameter",
        adapter="json_schema",
        schema={
            "type": "object",
            "properties": {"value": {"type": "integer"}},
            "required": ["value"],
            "additionalProperties": False,
        },
    )
    output_definition = ContractDefinition(
        id="parameter-value",
        version="1.0.0",
        description="One integer",
        adapter="json_schema",
        schema={"type": "integer"},
    )
    return {
        input_definition.id: build_contract_adapter(input_definition),
        output_definition.id: build_contract_adapter(output_definition),
    }


class IncrementTool(Tool):
    name = "increment"
    description = "Adds one"
    side_effect = "pure_read"
    parallel_safe = True

    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self, value: int) -> dict[str, int]:
        self.calls += 1
        return {"value": value + 1}


def _factory(
    tmp_path: Path,
    tool: Tool,
) -> CapabilityToolAdapterFactory:
    input_contract = _contract("number-input")
    output_contract = _contract("number-output")
    return CapabilityToolAdapterFactory(
        "test-runtime",
        {"increment": lambda arguments: tool(**arguments)},
        {
            input_contract.id: build_contract_adapter(input_contract),
            output_contract.id: build_contract_adapter(output_contract),
        },
        RunToolResultIndex(tmp_path, "run-neutral"),
    )


@pytest.mark.asyncio
async def test_definition_binds_current_tool_with_explicit_contracts_and_outcome(
    tmp_path: Path,
) -> None:
    tool = IncrementTool()
    adapter = _factory(tmp_path, tool).build(_definition())

    outcome = await adapter.invoke({"value": 1}, task_id="invoke-increment")

    assert outcome == ToolInvocationOutcome(
        status="ok",
        result={"value": 2},
        reused=False,
    )
    assert adapter.side_effect == "pure_read"
    assert adapter.parallel_safe is True
    assert adapter.reuse_result is True
    assert tool.calls == 1


@pytest.mark.asyncio
async def test_completed_tool_result_is_reused_through_existing_run_index(
    tmp_path: Path,
) -> None:
    tool = IncrementTool()
    adapter = _factory(tmp_path, tool).build(_definition())

    first = await adapter.invoke({"value": 1}, task_id="invoke-increment")
    second = await adapter.invoke({"value": 1}, task_id="invoke-increment")

    assert first.result == second.result == {"value": 2}
    assert first.reused is False
    assert second.reused is True
    assert tool.calls == 1


@pytest.mark.asyncio
async def test_input_contract_fails_before_current_tool_is_called(tmp_path: Path) -> None:
    tool = IncrementTool()
    adapter = _factory(tmp_path, tool).build(_definition())

    with pytest.raises(ContractValidationError):
        await adapter.invoke({"value": "not-an-integer"}, task_id="invoke-increment")

    assert tool.calls == 0


@pytest.mark.asyncio
async def test_capability_tool_reference_resolves_declared_callable(
    tmp_path: Path,
) -> None:
    calls: list[dict[str, int]] = []

    def normalize_parameter(arguments: dict[str, int]) -> int:
        calls.append(arguments)
        return arguments["value"]

    adapter = CapabilityToolAdapterFactory(
        "parameter-adjustment",
        {"normalize-parameter": normalize_parameter},
        _capability_contracts(),
    ).build(_capability_definition(
        implementation="capability:parameter-adjustment:normalize-parameter",
    ))

    outcome = await adapter.invoke({"value": 4}, task_id="normalize")

    assert outcome.status == "ok"
    assert outcome.result == 4
    assert calls == [{"value": 4}]


@pytest.mark.parametrize(
    "implementation",
    [
        "capability:parameter-adjustment:changed-tool",
        "capability:other-capability:normalize-parameter",
        "invalid-reference",
    ],
)
def test_capability_tool_reference_fails_before_runtime_assembly(
    implementation: str,
) -> None:
    factory = CapabilityToolAdapterFactory(
        "parameter-adjustment",
        {"normalize-parameter": lambda arguments: arguments["value"]},
        _capability_contracts(),
    )

    with pytest.raises(ToolAdapterError, match="capability Tool"):
        factory.build(_capability_definition(implementation=implementation))


@pytest.mark.asyncio
async def test_output_contract_validates_current_tool_result(tmp_path: Path) -> None:
    class InvalidOutputTool(IncrementTool):
        async def __call__(self, value: int) -> dict[str, str]:
            self.calls += 1
            return {"value": "not-an-integer"}

    tool = InvalidOutputTool()
    adapter = _factory(tmp_path, tool).build(_definition(reuse_result=False))

    with pytest.raises(ContractValidationError):
        await adapter.invoke({"value": 1}, task_id="invoke-increment")

    assert tool.calls == 1


@pytest.mark.asyncio
async def test_existing_file_tool_runs_through_declarative_adapter(
    tmp_path: Path,
) -> None:
    (tmp_path / "note.txt").write_text("hello\n", encoding="utf-8")
    tool = ReadTool(tmp_path)
    input_definition = ContractDefinition(
        id="read-input",
        version="1.0.0",
        description="Read input",
        adapter="json_schema",
        schema={
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
            "additionalProperties": False,
        },
    )
    output_definition = ContractDefinition(
        id="read-output",
        version="1.0.0",
        description="Read output",
        adapter="json_schema",
        schema={
            "type": "object",
            "properties": {
                "path": {"type": "string"},
                "content": {"type": "string"},
                "line_count": {"type": "integer"},
            },
            "required": ["path", "content", "line_count"],
        },
    )
    adapter = CapabilityToolAdapterFactory(
        "test-runtime",
        {"read": lambda arguments: tool(**arguments)},
        {
            input_definition.id: build_contract_adapter(input_definition),
            output_definition.id: build_contract_adapter(output_definition),
        },
    ).build(
        ToolDefinition(
            id="read-text",
            version="1.0.0",
            description="Reads text",
            implementation="capability:test-runtime:read",
            input_contract=input_definition.id,
            output_contract=output_definition.id,
            side_effect="pure_read",
            parallel_safe=True,
        )
    )

    outcome = await adapter.invoke({"path": "note.txt"}, task_id="read-note")

    assert outcome.status == "ok"
    assert outcome.result["content"] == "hello\n"


@pytest.mark.asyncio
async def test_current_tool_adapter_executes_through_invoke_tool_action(
    tmp_path: Path,
) -> None:
    tool = IncrementTool()
    factory = _factory(tmp_path, tool)
    definition = _definition(reuse_result=False)
    input_contract = _contract("number-input")
    output_contract = _contract("number-output")
    definitions = DefinitionRegistry()
    definitions.register(input_contract)
    definitions.register(output_contract)
    definitions.register(definition)
    workflow = WorkflowDefinition(
        id="invoke-current-tool",
        version="1.0.0",
        description="Invoke one current Tool through the declarative runtime",
        output_contract=output_contract.id,
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
                "tool": definition.id,
                "input_variable": "input",
                "output_variable": "incremented",
            },
            {
                "id": "finish",
                "kind": "end_workflow",
                "output_variable": "incremented",
                "output_name": "result",
            },
        ],
    )
    executors = build_builtin_executor_registry()
    plan = WorkflowCompiler(executors).compile(workflow, definitions)
    state = WorkflowState.for_plan("run-neutral", plan)

    completed = await _runtime(
        executors,
        FileWorkflowStateStore(tmp_path),
    ).execute(
        plan,
        state,
            RuntimeContext(
                tools={definition.id: factory.build(definition)},
                contracts={
                    input_contract.id: build_contract_adapter(input_contract),
                    output_contract.id: build_contract_adapter(output_contract),
                },
            ),
    )

    assert completed.outputs == {"result": {"value": 2}}
    assert completed.actions["invoke-increment"].output["status"] == "ok"

from pathlib import Path

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
from manyselves.kernel.workflow import (
    ActionExecutionStatus,
    WorkflowCompiler,
    WorkflowState,
    WorkflowStatus,
    resume_waiting_input,
)
from manyselves.runtime.state_store import FileWorkflowStateStore

FIXTURE = (
    Path(__file__).parents[2]
    / "fixtures"
    / "capabilities"
    / "interaction_output"
    / "capability.yaml"
)


@pytest.mark.asyncio
async def test_request_input_waits_resumes_and_publishes_declared_output(
    tmp_path: Path,
) -> None:
    _capability, registry = load_capability(FIXTURE)
    workflow = registry.require(DefinitionKind.WORKFLOW, "collect-name")
    assert isinstance(workflow, WorkflowDefinition)
    contracts = {
        definition.id: build_contract_adapter(definition)
        for definition in registry.all(DefinitionKind.CONTRACT)
        if isinstance(definition, ContractDefinition)
    }
    executors = build_builtin_executor_registry()
    plan = WorkflowCompiler(executors).compile(workflow, registry)
    store = FileWorkflowStateStore(tmp_path)
    runtime = ControlFlowWorkflowExecutor(executors, store)

    waiting = await runtime.execute(
        plan,
        WorkflowState.for_plan("interaction-run", plan),
        RuntimeContext(contracts=contracts, definitions=registry),
    )

    assert waiting.status is WorkflowStatus.WAITING
    assert waiting.next_action_id == "ask-name"
    assert waiting.actions["ask-name"].status is ActionExecutionStatus.WAITING
    assert waiting.waiting_input == {
        "input_id": "ask-name",
        "interaction_id": "request-name",
        "contract_id": "name-input",
        "title": "Your name",
        "description": "Collect one user name",
        "schema": {
            "type": "object",
            "properties": {"name": {"type": "string"}},
            "required": ["name"],
            "additionalProperties": False,
        },
    }

    resumed = resume_waiting_input(
        plan,
        waiting,
        input_id="ask-name",
        values={"name": "Ada"},
        contracts=contracts,
    )
    completed = await runtime.execute(
        plan,
        resumed,
        RuntimeContext(contracts=contracts, definitions=registry),
    )

    assert completed.status is WorkflowStatus.COMPLETED
    assert completed.waiting_input is None
    assert completed.outputs == {"result": {"name": "Ada"}}
    assert completed.actions["publish-name"].output == {"name": "Ada"}

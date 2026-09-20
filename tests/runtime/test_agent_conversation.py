"""Neutral Agent conversation ownership through the Generic Workflow Host."""

from pathlib import Path

import pytest

from manyselves.kernel.contracts import build_contract_adapter
from manyselves.kernel.conversations import ConversationKey, ConversationRegistry
from manyselves.kernel.definitions import (
    AgentDefinition,
    ContractDefinition,
    DefinitionRegistry,
    TaskDefinition,
    WorkflowDefinition,
)
from manyselves.kernel.executors import (
    RuntimeContext,
    RuntimeExecutionError,
    build_builtin_executor_registry,
)
from manyselves.kernel.ports import AgentInvocationOutcome
from manyselves.kernel.workflow import WorkflowCompiler, WorkflowState
from manyselves.runtime.conversation_store import FileConversationStore
from manyselves.runtime.state_store import FileWorkflowStateStore
from manyselves.runtime.workflow_host import (
    InMemoryWorkflowEventSink,
    WorkflowRuntimeHost,
)


class OutcomeOnlyAgentInvoker:
    """Return a Provider session without mutating the Kernel conversation."""

    async def invoke(
        self,
        agent,
        task,
        value,
        conversation,
        *,
        task_id: str,
    ) -> AgentInvocationOutcome:
        del agent, task, task_id
        assert conversation.external_session_id is None
        return AgentInvocationOutcome(
            status="ok",
            result={"value": value["value"] + 1},
            session_id="session-from-outcome",
        )


def _definitions() -> tuple[
    DefinitionRegistry,
    ContractDefinition,
    AgentDefinition,
    TaskDefinition,
]:
    input_contract = ContractDefinition(
        id="number-input",
        version="1.0.0",
        description="One input number",
        adapter="json_schema",
        schema={
            "type": "object",
            "properties": {"value": {"type": "integer"}},
            "required": ["value"],
            "additionalProperties": False,
        },
    )
    output_contract = input_contract.model_copy(
        update={"id": "number-output", "description": "One output number"}
    )
    agent = AgentDefinition(
        id="agent-a",
        version="1.0.0",
        description="Neutral Agent",
        instructions="Return a structured value.",
        accepts=[input_contract.id],
        produces=[output_contract.id],
        conversation_mode="run",
    )
    task = TaskDefinition(
        id="task-a",
        version="1.0.0",
        description="Increment one number",
        agent=agent.id,
        objective="Increment a neutral number.",
        input_contract=input_contract.id,
        output_contract=output_contract.id,
    )
    definitions = DefinitionRegistry()
    for definition in (input_contract, output_contract, agent, task):
        definitions.register(definition)
    return definitions, output_contract, agent, task


def _workflow(
    *,
    agent: AgentDefinition,
    task: TaskDefinition,
    output_contract: ContractDefinition,
    conversation_agent_id: str = "agent-a",
) -> WorkflowDefinition:
    return WorkflowDefinition(
        id="persist-agent-session",
        version="1.0.0",
        description="Persist the external session returned by an Agent invocation",
        tasks=[task.id],
        output_contract=output_contract.id,
        state={
            "input": {"value": 1},
            "conversation": {
                "conversation_id": (
                    f"persistent:persistent:{conversation_agent_id}:topic"
                ),
                "key": {
                    "agent_id": conversation_agent_id,
                    "value": "topic",
                    "mode": "persistent",
                },
                "run_id": "run-1",
                "external_session_id": None,
            },
        },
        actions=[
            {
                "id": "invoke-agent",
                "kind": "invoke_agent",
                "agent": agent.id,
                "task": task.id,
                "conversation_variable": "conversation",
                "input_variable": "input",
                "output_variable": "agent-output",
            },
            {
                "id": "finish",
                "kind": "end_workflow",
                "output_variable": "agent-output",
                "output_name": "result",
            },
        ],
    )


@pytest.mark.asyncio
async def test_invoke_agent_persists_the_provider_session_on_the_conversation(
    tmp_path: Path,
) -> None:
    definitions, output_contract, agent, task = _definitions()
    workflow = _workflow(
        agent=agent,
        task=task,
        output_contract=output_contract,
    )
    definitions.register(workflow)
    executors = build_builtin_executor_registry()
    plan = WorkflowCompiler(executors).compile(workflow, definitions)
    store = FileConversationStore(tmp_path)
    conversations = ConversationRegistry(store)

    completed = await WorkflowRuntimeHost(
        executors,
        FileWorkflowStateStore(tmp_path),
        InMemoryWorkflowEventSink(),
    ).execute(
        plan,
        WorkflowState.for_plan("run-1", plan),
        RuntimeContext(
            agents={agent.id: OutcomeOnlyAgentInvoker()},
            definitions=definitions,
            contracts={
                definition.id: build_contract_adapter(definition)
                for definition in definitions.all()
                if isinstance(definition, ContractDefinition)
            },
            conversations=conversations,
        ),
    )

    restored = ConversationRegistry(store).resolve(
        ConversationKey(agent_id=agent.id, value="topic", mode="persistent"),
        run_id="another-run",
    )

    assert restored is not None
    assert restored.external_session_id == "session-from-outcome"
    assert completed.variables["conversation"].external_session_id == (
        "session-from-outcome"
    )


@pytest.mark.asyncio
async def test_invoke_agent_rejects_a_persisted_conversation_for_another_agent(
    tmp_path: Path,
) -> None:
    definitions, output_contract, agent, task = _definitions()
    workflow = _workflow(
        agent=agent,
        task=task,
        output_contract=output_contract,
        conversation_agent_id="agent-b",
    )
    definitions.register(workflow)
    executors = build_builtin_executor_registry()
    plan = WorkflowCompiler(executors).compile(workflow, definitions)

    with pytest.raises(
        RuntimeExecutionError,
        match="conversation belongs to agent agent-b, not agent-a",
    ):
        await WorkflowRuntimeHost(
            executors,
            FileWorkflowStateStore(tmp_path),
            InMemoryWorkflowEventSink(),
        ).execute(
            plan,
            WorkflowState.for_plan("run-1", plan),
            RuntimeContext(
                agents={agent.id: OutcomeOnlyAgentInvoker()},
                definitions=definitions,
                contracts={
                    definition.id: build_contract_adapter(definition)
                    for definition in definitions.all()
                    if isinstance(definition, ContractDefinition)
                },
            ),
        )

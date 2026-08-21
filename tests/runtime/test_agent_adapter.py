from pathlib import Path
from types import SimpleNamespace

import pytest

from manyselves.core.reporting.config import AgentDefinition as ReportingAgentDefinition
from manyselves.kernel.contracts import build_contract_adapter
from manyselves.kernel.conversations import ConversationKey, ConversationRegistry
from manyselves.kernel.definitions import (
    AgentDefinition,
    ContractDefinition,
    DefinitionRegistry,
    RecoveryPolicyDefinition,
    RecoveryRule,
    TaskDefinition,
    WorkflowDefinition,
)
from manyselves.kernel.executors import (
    RuntimeContext,
    RuntimeExecutionError,
    SequentialWorkflowExecutor,
    build_builtin_executor_registry,
)
from manyselves.kernel.recovery import (
    RecoveryActionKind,
    RecoveryController,
    RecoveryEvent,
    RecoveryEventKind,
    RecoveryState,
)
from manyselves.kernel.workflow import WorkflowCompiler, WorkflowState
from manyselves.runtime.agent_adapter import LegacyReportingAgentAdapter
from manyselves.runtime.conversation_store import FileConversationStore
from manyselves.runtime.state_store import FileWorkflowStateStore


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


def _agent(agent_id: str) -> AgentDefinition:
    return AgentDefinition(
        id=agent_id,
        version="1.0.0",
        description=f"Agent {agent_id}",
        instructions="Return a structured value.",
        accepts=["number-input"],
        produces=["number-output"],
        conversation_mode="run",
    )


def _task(
    task_id: str,
    agent_id: str,
    *,
    recovery: str | None = None,
) -> TaskDefinition:
    return TaskDefinition(
        id=task_id,
        version="1.0.0",
        description=f"Task {task_id}",
        agent=agent_id,
        objective="Increment a neutral number.",
        input_contract="number-input",
        output_contract="number-output",
        recovery=recovery,
    )


def _reporting_agent(agent_id: str, tmp_path: Path) -> ReportingAgentDefinition:
    return ReportingAgentDefinition(
        name=agent_id,
        description=f"Legacy {agent_id}",
        instructions="Return a structured value.",
        source_path=tmp_path / f"{agent_id}.md",
    )


class FakeReportingRunner:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.sessions: dict[tuple[str, str], str] = {}

    async def run(
        self,
        definition,
        envelope,
        shared_artifacts,
        *,
        workflow_id: str,
        session_key: str | None = None,
    ):
        identity = (definition.id, str(session_key))
        session_id = self.sessions.setdefault(identity, f"session-{len(self.sessions) + 1}")
        self.calls.append(
            {
                "definition": definition,
                "envelope": envelope,
                "workflow_id": workflow_id,
                "session_key": session_key,
                "shared_artifacts": shared_artifacts,
            }
        )
        return SimpleNamespace(
            status="completed",
            session_id=session_id,
            payload={"value": envelope.input_value["value"] + 1},
            reason=None,
        )


class RecoveryAwareReportingRunner(FakeReportingRunner):
    def __init__(self) -> None:
        super().__init__()
        self.recovery_policies: list[RecoveryPolicyDefinition | None] = []
        self.recovery_decisions = []

    async def run(
        self,
        definition,
        envelope,
        shared_artifacts,
        *,
        workflow_id: str,
        session_key: str | None = None,
        recovery_policy: RecoveryPolicyDefinition | None = None,
    ):
        self.recovery_policies.append(recovery_policy)
        if recovery_policy is not None:
            decision = RecoveryController().decide(
                RecoveryEvent(kind=RecoveryEventKind.COMPLETED_TOOL_RESULT),
                recovery_policy,
                RecoveryState(),
            )
            self.recovery_decisions.append(decision)
        return await super().run(
            definition,
            envelope,
            shared_artifacts,
            workflow_id=workflow_id,
            session_key=session_key,
        )


def _adapter(tmp_path: Path, runner: FakeReportingRunner, agent_ids: list[str]):
    return LegacyReportingAgentAdapter(
        runner,
        {agent_id: _reporting_agent(agent_id, tmp_path) for agent_id in agent_ids},
        envelope_factory=lambda agent, task, value, conversation, task_id: SimpleNamespace(
            task_id=task_id,
            run_id=conversation.run_id,
            agent_id=agent.id,
            objective=task.objective,
            input_value=value,
        ),
        workflow_id="workflow-neutral",
    )


@pytest.mark.asyncio
async def test_legacy_adapter_binds_two_agent_and_task_definitions_without_history_sharing(
    tmp_path: Path,
) -> None:
    runner = FakeReportingRunner()
    adapter = _adapter(tmp_path, runner, ["agent-a", "agent-b"])
    registry = ConversationRegistry()
    task_a = _task("task-a", "agent-a")
    task_b = _task("task-b", "agent-b")
    conversation_a = registry.create_or_resolve(
        ConversationKey(agent_id="agent-a", value="same-key", mode="run"),
        run_id="run-1",
    )
    conversation_b = registry.create_or_resolve(
        ConversationKey(agent_id="agent-b", value="same-key", mode="run"),
        run_id="run-1",
    )

    first = await adapter.invoke(
        _agent("agent-a"), task_a, {"value": 1}, conversation_a, task_id="invoke-a"
    )
    second = await adapter.invoke(
        _agent("agent-b"), task_b, {"value": 1}, conversation_b, task_id="invoke-b"
    )

    assert first.result == second.result == {"value": 2}
    assert first.session_id != second.session_id
    assert [call["session_key"] for call in runner.calls] == ["same-key", "same-key"]


@pytest.mark.asyncio
async def test_same_conversation_key_reuses_original_legacy_session(tmp_path: Path) -> None:
    runner = FakeReportingRunner()
    adapter = _adapter(tmp_path, runner, ["agent-a"])
    registry = ConversationRegistry()
    agent = _agent("agent-a")
    task = _task("task-a", "agent-a")
    first_record = registry.create_or_resolve(
        ConversationKey(agent_id=agent.id, value="topic", mode="run"),
        run_id="run-1",
    )
    second_record = registry.create_or_resolve(
        ConversationKey(agent_id=agent.id, value="topic", mode="run"),
        run_id="run-1",
    )

    first = await adapter.invoke(agent, task, {"value": 1}, first_record, task_id="first")
    second = await adapter.invoke(agent, task, {"value": 2}, second_record, task_id="second")

    assert first.session_id == second.session_id
    assert second_record.external_session_id == first.session_id


@pytest.mark.asyncio
async def test_invoke_agent_action_runs_through_legacy_adapter(tmp_path: Path) -> None:
    input_contract = _contract("number-input")
    output_contract = _contract("number-output")
    agent = _agent("agent-a")
    task = _task("task-a", agent.id)
    definitions = DefinitionRegistry()
    for definition in (input_contract, output_contract, agent, task):
        definitions.register(definition)
    workflow = WorkflowDefinition(
        id="invoke-current-agent",
        version="1.0.0",
        description="Invoke one current Agent through the declarative runtime",
        tasks=[task.id],
        output_contract=output_contract.id,
        actions=[
            {
                "id": "set-input",
                "kind": "set_variable",
                "variable": "input",
                "value": {"value": 1},
            },
            {
                "id": "create-conversation",
                "kind": "create_conversation",
                "agent": agent.id,
                "conversation_key": "topic",
                "mode": "run",
                "output_variable": "conversation",
            },
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
    runner = FakeReportingRunner()
    adapter = _adapter(tmp_path, runner, [agent.id])
    executors = build_builtin_executor_registry()
    plan = WorkflowCompiler(executors).compile(workflow, definitions)
    state = WorkflowState.for_plan("run-1", plan)

    completed = await SequentialWorkflowExecutor(
        executors,
        FileWorkflowStateStore(tmp_path),
    ).execute(
        plan,
        state,
        RuntimeContext(
            agents={agent.id: adapter},
            definitions=definitions,
            contracts={
                input_contract.id: build_contract_adapter(input_contract),
                output_contract.id: build_contract_adapter(output_contract),
            },
        ),
    )

    assert completed.outputs == {"result": {"value": 2}}
    assert completed.conversations["conversation"].external_session_id == "session-1"
    assert runner.calls[0]["session_key"] == "topic"


@pytest.mark.asyncio
async def test_invoke_agent_persists_the_provider_session_on_the_conversation(
    tmp_path: Path,
) -> None:
    input_contract = _contract("number-input")
    output_contract = _contract("number-output")
    agent = _agent("agent-a")
    task = _task("task-a", agent.id)
    definitions = DefinitionRegistry()
    for definition in (input_contract, output_contract, agent, task):
        definitions.register(definition)
    workflow = WorkflowDefinition(
        id="persist-agent-session",
        version="1.0.0",
        description="Persist the external session returned by an Agent invocation",
        tasks=[task.id],
        output_contract=output_contract.id,
        actions=[
            {
                "id": "set-input",
                "kind": "set_variable",
                "variable": "input",
                "value": {"value": 1},
            },
            {
                "id": "create-conversation",
                "kind": "create_conversation",
                "agent": agent.id,
                "conversation_key": "topic",
                "mode": "persistent",
                "output_variable": "conversation",
            },
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
    runner = FakeReportingRunner()
    store = FileConversationStore(tmp_path)
    conversations = ConversationRegistry(store)
    executors = build_builtin_executor_registry()
    plan = WorkflowCompiler(executors).compile(workflow, definitions)
    completed = await SequentialWorkflowExecutor(
        executors,
        FileWorkflowStateStore(tmp_path),
    ).execute(
        plan,
        WorkflowState.for_plan("run-1", plan),
        RuntimeContext(
            agents={agent.id: _adapter(tmp_path, runner, [agent.id])},
            definitions=definitions,
            contracts={
                input_contract.id: build_contract_adapter(input_contract),
                output_contract.id: build_contract_adapter(output_contract),
            },
            conversations=conversations,
        ),
    )

    restored = ConversationRegistry(store).resolve(
        ConversationKey(agent_id=agent.id, value="topic", mode="persistent"),
        run_id="another-run",
    )

    assert restored is not None
    assert restored.external_session_id == "session-1"
    assert (
        completed.variables["conversation"].external_session_id
        == "session-1"
    )


@pytest.mark.asyncio
async def test_invoke_agent_rejects_a_persisted_conversation_for_another_agent(
    tmp_path: Path,
) -> None:
    input_contract = _contract("number-input")
    output_contract = _contract("number-output")
    agent = _agent("agent-a")
    task = _task("task-a", agent.id)
    conversation = ConversationRegistry().create_or_resolve(
        ConversationKey(agent_id="agent-b", value="topic", mode="run"),
        run_id="run-1",
    )
    definitions = DefinitionRegistry()
    for definition in (input_contract, output_contract, agent, task):
        definitions.register(definition)
    workflow = WorkflowDefinition(
        id="reject-cross-agent-conversation",
        version="1.0.0",
        description="Preserve the declared Agent conversation identity",
        tasks=[task.id],
        state={
            "input": {"value": 1},
            "conversation": conversation.model_dump(mode="json"),
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
            },
        ],
    )
    executors = build_builtin_executor_registry()
    plan = WorkflowCompiler(executors).compile(workflow, definitions)

    with pytest.raises(
        RuntimeExecutionError,
        match="conversation belongs to agent agent-b, not agent-a",
    ):
        await SequentialWorkflowExecutor(
            executors,
            FileWorkflowStateStore(tmp_path),
        ).execute(
            plan,
            WorkflowState.for_plan("run-1", plan),
            RuntimeContext(
                agents={agent.id: _adapter(tmp_path, FakeReportingRunner(), [agent.id])},
                definitions=definitions,
                contracts={
                    input_contract.id: build_contract_adapter(input_contract),
                    output_contract.id: build_contract_adapter(output_contract),
                },
            ),
        )


@pytest.mark.asyncio
async def test_invoke_agent_resolves_task_recovery_through_the_generic_port(
    tmp_path: Path,
) -> None:
    input_contract = _contract("number-input")
    output_contract = _contract("number-output")
    agent = _agent("agent-a")
    recovery = RecoveryPolicyDefinition(
        id="agent-recovery",
        version="1.0.0",
        description="Reuse a completed tool result.",
        rules={
            RecoveryEventKind.COMPLETED_TOOL_RESULT.value: RecoveryRule(
                action=RecoveryActionKind.REUSE_RESULT.value,
            )
        },
    )
    task = _task("task-a", agent.id, recovery=recovery.id)
    definitions = DefinitionRegistry()
    for definition in (input_contract, output_contract, agent, recovery, task):
        definitions.register(definition)
    workflow = WorkflowDefinition(
        id="invoke-agent-with-recovery",
        version="1.0.0",
        description="Invoke one Agent with a declared recovery policy.",
        tasks=[task.id],
        output_contract=output_contract.id,
        actions=[
            {
                "id": "set-input",
                "kind": "set_variable",
                "variable": "input",
                "value": {"value": 1},
            },
            {
                "id": "create-conversation",
                "kind": "create_conversation",
                "agent": agent.id,
                "conversation_key": "topic",
                "mode": "run",
                "output_variable": "conversation",
            },
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
    runner = RecoveryAwareReportingRunner()
    adapter = _adapter(tmp_path, runner, [agent.id])
    assert callable(getattr(adapter, "invoke_with_recovery", None))
    executors = build_builtin_executor_registry()
    plan = WorkflowCompiler(executors).compile(workflow, definitions)

    completed = await SequentialWorkflowExecutor(
        executors,
        FileWorkflowStateStore(tmp_path),
    ).execute(
        plan,
        WorkflowState.for_plan("run-recovery", plan),
        RuntimeContext(
            agents={agent.id: adapter},
            definitions=definitions,
            contracts={
                input_contract.id: build_contract_adapter(input_contract),
                output_contract.id: build_contract_adapter(output_contract),
            },
        ),
    )

    assert completed.outputs == {"result": {"value": 2}}
    assert runner.recovery_policies == [recovery]
    assert len(runner.recovery_decisions) == 1
    assert runner.recovery_decisions[0].action is RecoveryActionKind.REUSE_RESULT
    assert completed.conversations["conversation"].external_session_id == "session-1"
    assert runner.calls[0]["session_key"] == "topic"

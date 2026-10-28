"""An authentication failure stays "not retryable" from the Agent loop to the Run."""

import asyncio

import pytest

from manyselves.config.schema import AgentDefaults
from manyselves.runtime.agent_execution import AgentExecutionService, AgentTurnRequest
from manyselves.runtime.loops.agent_loop import AgentLoop
from manyselves.runtime.loops.bus import MessageBus
from manyselves.runtime.providers.base import LLMProvider
from manyselves.runtime.run_failure import describe_failure
from manyselves.runtime.tools.registry import ToolRegistry
from manyselves.runtime.typed_agent_turn import TypedAgentTurn


class AuthenticationError(Exception):
    status_code = 401


class RejectingProvider(LLMProvider):
    """An ordinary Provider whose key is no longer valid."""

    def __init__(self) -> None:
        super().__init__("expired-key", model="any-model")
        self.calls = 0

    async def chat(self, messages, tools=None, temperature=0.1, max_tokens=8192, **_):
        self.calls += 1
        raise AuthenticationError("Error code: 401 - invalid x-api-key")

    async def chat_stream(self, *args, **kwargs):
        self.calls += 1
        raise AuthenticationError("Error code: 401 - invalid x-api-key")
        yield  # pragma: no cover - makes this an async generator


async def _run_once(tmp_path, provider):
    from manyselves.kernel.contracts import build_contract_adapter
    from manyselves.kernel.definitions import (
        AgentDefinition,
        ContractDefinition,
        DefinitionRegistry,
        TaskDefinition,
        WorkflowDefinition,
    )
    from manyselves.kernel.executors import RuntimeContext, build_builtin_executor_registry
    from manyselves.kernel.workflow import WorkflowCompiler, WorkflowState
    from manyselves.runtime.run_lifecycle import StartAwareFileWorkflowStateStore
    from manyselves.runtime.workflow_host import InMemoryWorkflowEventSink, WorkflowRuntimeHost

    contract = ContractDefinition(
        id="value", version="1.0.0", description="Any value",
        adapter="json_schema", schema={},
    )
    agent = AgentDefinition(
        id="agent-a", version="1.0.0", description="Neutral Agent",
        instructions="Answer.", accepts=["value"], produces=["value"],
        conversation_mode="run",
    )
    task = TaskDefinition(
        id="task-a", version="1.0.0", description="Answer", agent="agent-a",
        objective="Answer.", input_contract="value", output_contract="value",
    )
    workflow = WorkflowDefinition(
        id="one-turn", version="1.0.0", description="One Agent turn",
        tasks=["task-a"], state={"input": "go"},
        actions=[
            {"id": "conversation", "kind": "create_conversation", "agent": "agent-a",
             "conversation_key": "c", "mode": "run", "output_variable": "conversation"},
            {"id": "turn", "kind": "invoke_agent", "agent": "agent-a", "task": "task-a",
             "conversation_variable": "conversation", "input_variable": "input",
             "output_variable": "output"},
            {"id": "finish", "kind": "end_workflow", "output_variable": "output"},
        ],
    )
    registry = DefinitionRegistry()
    for definition in (contract, agent, task, workflow):
        registry.register(definition)
    executors = build_builtin_executor_registry()
    plan = WorkflowCompiler(executors).compile(workflow, registry)

    bus = MessageBus()
    bus_task = asyncio.create_task(bus.process_queue())
    service = AgentExecutionService(bus, timeout=5)

    class TurnInvoker:
        """One real Agent loop turn, mapped like the production typed turn."""

        async def invoke(self, agent, task, value, conversation, *, task_id):
            session = await service.start_or_restore(
                workflow_id="wf", conversation_key="c", runtime_id="agent-a",
                session_id="session-a",
                session_factory=lambda: AgentLoop(
                    agent_type="agent-a", workspace=tmp_path, tools=ToolRegistry(),
                    bus=bus, config=AgentDefaults(), llm_provider=provider,
                ),
            )
            outcome = await service.dispatch_turn(session, AgentTurnRequest(
                content="answer", message_id="m1", workflow_id="wf",
                run_id="run-1", task_id=task_id, task_attempt_id=task_id,
            ))
            return TypedAgentTurn.map_outcome(
                outcome, session_id=session.session_id, decode_result=lambda ref: ref
            )

    store = StartAwareFileWorkflowStateStore(tmp_path)
    host = WorkflowRuntimeHost(executors, store, InMemoryWorkflowEventSink())
    try:
        with pytest.raises(Exception):
            await host.execute(
                plan,
                WorkflowState.for_plan("run-1", plan),
                RuntimeContext(
                    agents={"agent-a": TurnInvoker()},
                    contracts={"value": build_contract_adapter(contract)},
                    definitions=registry,
                ),
            )
        state = store.load("run-1")
        return plan, state
    finally:
        await service.close_workflow("wf")
        bus.shutdown()
        await bus_task


@pytest.mark.asyncio
async def test_authentication_failure_reaches_the_run_as_not_retryable(tmp_path) -> None:
    provider = RejectingProvider()

    plan, state = await _run_once(tmp_path, provider)

    record = state.actions["turn"]
    assert record.failure == {"category": "authentication", "retryable": False}
    failure = describe_failure(plan, state.model_dump(mode="json"))
    assert failure is not None
    assert (failure.category, failure.retryable) == ("provider", False)
    # The loop itself did not retry an authentication failure either.
    assert provider.calls == 1


def test_runs_recorded_without_a_verdict_still_use_the_error_text() -> None:
    from manyselves.runtime.run_failure import _classify

    assert _classify("invoke_agent", "HTTP 529 overloaded") == ("temporary", True)
    assert _classify("invoke_agent", "no usable result") == ("model", True)
    assert _classify(
        "invoke_agent", "anything", {"category": "rate_limit", "retryable": True}
    ) == ("temporary", True)
    assert _classify(
        "invoke_agent", "anything", {"category": "malformed_output", "retryable": True}
    ) == ("model", True)

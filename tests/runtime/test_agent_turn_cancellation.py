"""Cancelling the caller of an Agent turn stops that turn's model and tool work."""

import asyncio

import pytest

from manyselves.config.schema import AgentDefaults
from manyselves.runtime.agent_execution import AgentExecutionService, AgentTurnRequest
from manyselves.runtime.loops.agent_loop import AgentLoop
from manyselves.runtime.loops.bus import MessageBus
from manyselves.runtime.providers.base import LLMProvider, LLMResponse, LLMToolCall
from manyselves.runtime.tools.registry import Tool, ToolRegistry


class SlowToolCallProvider(LLMProvider):
    """Answers after a delay with a call to the file-writing tool."""

    def __init__(self) -> None:
        super().__init__("key", model="slow-model")
        self.calls = 0

    async def chat(self, messages, tools=None, temperature=0.1, max_tokens=8192):
        self.calls += 1
        await asyncio.sleep(0.2)
        if self.calls == 1:
            return LLMResponse(
                content="",
                tool_calls=[LLMToolCall(id="write-1", name="write_marker", arguments={})],
            )
        return LLMResponse(content="done")


class WriteMarkerTool(Tool):
    name = "write_marker"
    description = "Write a marker file."

    def __init__(self, path) -> None:
        self.path = path

    async def __call__(self) -> dict:
        self.path.write_text("written", encoding="utf-8")
        return {"status": "ok"}


@pytest.mark.asyncio
async def test_cancelled_turn_does_not_run_tools_afterwards(tmp_path) -> None:
    bus = MessageBus()
    bus_task = asyncio.create_task(bus.process_queue())
    marker = tmp_path / "marker.txt"
    tools = ToolRegistry()
    tools.register(WriteMarkerTool(marker))
    provider = SlowToolCallProvider()
    service = AgentExecutionService(bus)
    try:
        session = await service.start_or_restore(
            workflow_id="wf",
            conversation_key="conv",
            runtime_id="agent-a",
            session_id="session-a",
            session_factory=lambda: AgentLoop(
                agent_type="agent-a", workspace=tmp_path, tools=tools, bus=bus,
                config=AgentDefaults(), llm_provider=provider,
            ),
        )
        turn = asyncio.create_task(service.dispatch_turn(session, AgentTurnRequest(
            content="write the marker", message_id="m1", workflow_id="wf",
            run_id="run", task_id="task", task_attempt_id="attempt",
        )))
        await asyncio.sleep(0.05)
        turn.cancel()
        with pytest.raises(asyncio.CancelledError):
            await turn
        await asyncio.sleep(0.6)
        assert not marker.exists()
    finally:
        await service.close_workflow("wf")
        bus.shutdown()
        await bus_task


@pytest.mark.asyncio
async def test_one_run_cancel_stops_parallel_agent_turns_through_the_host(tmp_path) -> None:
    """Run cancel from the lifecycle owner reaches every branch's Agent turn."""

    from manyselves.kernel.contracts import build_contract_adapter
    from manyselves.kernel.definitions import (
        AgentDefinition,
        ContractDefinition,
        DefinitionRegistry,
        TaskDefinition,
        WorkflowDefinition,
    )
    from manyselves.kernel.executors import RuntimeContext, build_builtin_executor_registry
    from manyselves.kernel.ports import AgentInvocationOutcome
    from manyselves.kernel.workflow import WorkflowCompiler, WorkflowState, WorkflowStatus
    from manyselves.runtime import run_lifecycle
    from manyselves.runtime.run_lifecycle import (
        DetachedRunTaskOwner,
        StartAwareFileWorkflowStateStore,
    )
    from manyselves.runtime.workflow_host import (
        RUN_CANCEL_REQUESTED,
        InMemoryWorkflowEventSink,
        WorkflowRuntimeHost,
    )

    contract = ContractDefinition(
        id="value", version="1.0.0", description="Any value",
        adapter="json_schema", schema={},
    )
    agent = AgentDefinition(
        id="agent-a", version="1.0.0", description="Neutral Agent",
        instructions="Write the marker.", accepts=["value"], produces=["value"],
        conversation_mode="run",
    )
    task = TaskDefinition(
        id="task-a", version="1.0.0", description="Write a marker", agent="agent-a",
        objective="Write the marker.", input_contract="value", output_contract="value",
    )
    branch = lambda name: [  # noqa: E731
        {"id": f"{name}-conversation", "kind": "create_conversation", "agent": "agent-a",
         "conversation_key": name, "mode": "run", "output_variable": f"{name}-conversation"},
        {"id": f"{name}-turn", "kind": "invoke_agent", "agent": "agent-a", "task": "task-a",
         "conversation_variable": f"{name}-conversation", "input_variable": "input",
         "output_variable": f"{name}-output"},
        {"id": f"{name}-done", "kind": "goto", "target": "join"},
    ]
    workflow = WorkflowDefinition(
        id="parallel-agent-cancel", version="1.0.0", description="Two Agent branches",
        tasks=["task-a"], state={"input": "go"},
        actions=[
            {"id": "parallel", "kind": "parallel",
             "branches": {"left": "left-conversation", "right": "right-conversation"},
             "join": "join"},
            *branch("left"), *branch("right"),
            {"id": "join", "kind": "join", "parallel": "parallel",
             "inputs": {"left": "left-output", "right": "right-output"},
             "output_variable": "joined"},
            {"id": "finish", "kind": "end_workflow", "output_variable": "joined"},
        ],
    )
    registry = DefinitionRegistry()
    for definition in (contract, agent, task, workflow):
        registry.register(definition)
    executors = build_builtin_executor_registry()
    plan = WorkflowCompiler(executors).compile(workflow, registry)

    bus = MessageBus()
    bus_task = asyncio.create_task(bus.process_queue())
    service = AgentExecutionService(bus)
    markers = {name: tmp_path / f"{name}.txt" for name in ("left", "right")}
    started = asyncio.Event()

    class TurnInvoker:
        """Minimal Capability bridge: one neutral Agent turn per invocation."""

        async def invoke(self, agent, task, value, conversation, *, task_id):
            name = conversation.key.value
            tools = ToolRegistry()
            tools.register(WriteMarkerTool(markers[name]))
            provider = SlowToolCallProvider()
            session = await service.start_or_restore(
                workflow_id="wf", conversation_key=name, runtime_id=f"agent-{name}",
                session_id=f"session-{name}",
                session_factory=lambda: AgentLoop(
                    agent_type=f"agent-{name}", workspace=tmp_path, tools=tools, bus=bus,
                    config=AgentDefaults(), llm_provider=provider,
                ),
            )
            started.set()
            await service.dispatch_turn(session, AgentTurnRequest(
                content="write the marker", message_id=f"{name}-m1", workflow_id="wf",
                run_id="cancel-run", task_id=task_id, task_attempt_id=task_id,
            ))
            return AgentInvocationOutcome(status="ok", result="done")

    store = StartAwareFileWorkflowStateStore(tmp_path)
    host = WorkflowRuntimeHost(executors, store, InMemoryWorkflowEventSink())
    owner = DetachedRunTaskOwner()
    original = run_lifecycle._CANCEL_REPEAT_SECONDS
    # A single cancel must be enough: a repeat would only come after this.
    run_lifecycle._CANCEL_REPEAT_SECONDS = 30.0
    try:
        await owner.accept_after_persisted_state(
            run_id="cancel-run",
            state_store=store,
            operation=host.execute(
                plan,
                WorkflowState.for_plan("cancel-run", plan),
                RuntimeContext(
                    agents={"agent-a": TurnInvoker()},
                    contracts={"value": build_contract_adapter(contract)},
                    definitions=registry,
                ),
            ),
        )
        await asyncio.wait_for(started.wait(), 5)
        await asyncio.sleep(0.05)
        settled = owner.cancel("cancel-run")
        assert settled is not None
        await asyncio.wait_for(settled, 10)
        await asyncio.sleep(0.6)

        state = store.load("cancel-run")
        assert state.status is WorkflowStatus.FAILED
        assert RUN_CANCEL_REQUESTED in (state.actions["parallel"].error or "")
        assert not any(marker.exists() for marker in markers.values())
    finally:
        run_lifecycle._CANCEL_REPEAT_SECONDS = original
        await service.close_workflow("wf")
        bus.shutdown()
        await bus_task

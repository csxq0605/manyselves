import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import pytest

from manyselves.core.loops.bus import MessageBus
from manyselves.interfaces.types import AgentResponse, AgentResultMessage, UserMessage
from manyselves.runtime.agent_execution import (
    AgentExecutionService,
    AgentSessionRestore,
    AgentTerminalSubscription,
    AgentTurnRequest,
)


@dataclass
class ScriptedAgentLoop:
    bus: MessageBus
    runtime_id: str
    responses: list[str]
    restored_messages: list[dict[str, Any]] = field(default_factory=list)
    started: int = 0
    stopped: int = 0
    completed_turns: int = 0
    _callback: Callable[[UserMessage], Any] | None = None

    def restore_conversation(
        self,
        messages,
        *,
        task_boundaries=(),
        handoff_summary=None,
    ) -> None:
        del task_boundaries, handoff_summary
        self.restored_messages = list(messages)

    async def start(self) -> None:
        self.started += 1

        async def respond(message: UserMessage) -> None:
            if message.agent_type != self.runtime_id:
                return
            if not self.responses:
                return
            self.completed_turns += 1
            await self.bus.publish(
                AgentResponse(
                    agent_type=self.runtime_id,
                    content=self.responses.pop(0),
                    message_id=message.message_id,
                    workflow_id=message.workflow_id,
                    run_id=message.run_id,
                    task_id=message.task_id,
                    task_attempt_id=message.task_attempt_id,
                    session_id=message.session_id,
                )
            )

        self._callback = respond
        self.bus.subscribe(UserMessage, respond)

    async def stop(self) -> None:
        self.stopped += 1
        if self._callback is not None:
            self.bus.unsubscribe(UserMessage, self._callback)

    async def wait_until_turn_complete(self) -> None:
        return None


@pytest.mark.asyncio
async def test_service_owns_session_restore_reuse_turn_and_close() -> None:
    bus = MessageBus()
    bus_task = asyncio.create_task(bus.process_queue())
    created: list[ScriptedAgentLoop] = []

    def factory() -> ScriptedAgentLoop:
        loop = ScriptedAgentLoop(bus, "runtime-agent", ["first", "second"])
        created.append(loop)
        return loop

    service = AgentExecutionService(bus, timeout=1)
    restore = AgentSessionRestore(
        messages=[{"role": "user", "content": "persisted"}],
        task_boundaries=[{"current": {"task_id": "old"}, "sequence": 1}],
    )

    first_session = await service.start_or_restore(
        workflow_id="workflow-1",
        conversation_key="agent-a",
        runtime_id="runtime-agent",
        session_id="provider-session-7",
        session_factory=factory,
        restore=restore,
    )
    reused_session = await service.start_or_restore(
        workflow_id="workflow-1",
        conversation_key="agent-a",
        runtime_id="runtime-agent",
        session_id="ignored-on-reuse",
        session_factory=factory,
    )

    assert reused_session.loop is first_session.loop
    assert reused_session.session_id == first_session.session_id == "provider-session-7"
    assert len(created) == 1
    assert created[0].started == 1
    assert created[0].restored_messages == [
        {"role": "user", "content": "persisted"}
    ]

    first = await service.dispatch_turn(
        first_session,
        AgentTurnRequest(
            content="one",
            message_id="task-1",
            workflow_id="workflow-1",
            run_id="run-1",
            task_id="task-1",
            task_attempt_id="attempt-1",
        ),
    )
    second = await service.dispatch_turn(
        reused_session,
        AgentTurnRequest(
            content="two",
            message_id="task-2",
            workflow_id="workflow-1",
            run_id="run-1",
            task_id="task-2",
            task_attempt_id="attempt-2",
            internal=True,
            turn_kind="submission_correction",
        ),
    )

    assert first.kind == second.kind == "response"
    assert first.session_id == second.session_id == "provider-session-7"
    assert first.message.content == "first"
    assert second.message.content == "second"

    await service.close_workflow("workflow-1")
    assert created[0].stopped == 1
    assert service.sessions == {}
    bus.shutdown()
    await bus_task


@pytest.mark.asyncio
async def test_dispatch_turn_returns_capability_terminal_before_natural_response() -> None:
    bus = MessageBus()
    bus_task = asyncio.create_task(bus.process_queue())
    loop = ScriptedAgentLoop(bus, "runtime-agent", [])
    service = AgentExecutionService(bus, timeout=1)
    session = await service.start_or_restore(
        workflow_id="workflow-1",
        conversation_key="agent-a",
        runtime_id="runtime-agent",
        session_id="provider-session-8",
        session_factory=lambda: loop,
    )

    async def publish_typed(message: UserMessage) -> None:
        if message.task_attempt_id != "attempt-1":
            return
        await bus.publish(
            AgentResultMessage(
                sender="runtime-agent",
                task_id=message.task_id,
                run_id=message.run_id,
                result_path="Work/results/typed.json",
                workflow_id=message.workflow_id,
                task_attempt_id=message.task_attempt_id,
                session_id=message.session_id,
            )
        )

    bus.subscribe(UserMessage, publish_typed)
    outcome = await service.dispatch_turn(
        session,
        AgentTurnRequest(
            content="one",
            message_id="task-1",
            workflow_id="workflow-1",
            run_id="run-1",
            task_id="task-1",
            task_attempt_id="attempt-1",
        ),
        terminals=(
            AgentTerminalSubscription(
                kind="typed_result",
                message_type=AgentResultMessage,
                predicate=lambda item: item.result_path == "Work/results/typed.json",
            ),
        ),
    )

    assert outcome.kind == "typed_result"
    assert outcome.session_id == "provider-session-8"
    assert outcome.message.result_path == "Work/results/typed.json"

    await service.close_workflow("workflow-1")
    bus.shutdown()
    await bus_task

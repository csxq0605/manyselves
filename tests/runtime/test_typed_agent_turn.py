"""Characterization for the business-neutral typed Agent turn primitive."""

from __future__ import annotations

import asyncio

import pytest

from manyselves.core.loops.bus import MessageBus
from manyselves.interfaces.types import AgentResultMessage, Error, UserMessage
from manyselves.runtime.agent_execution import (
    AgentExecutionService,
    AgentSessionRestore,
    AgentTurnRequest,
)


@pytest.mark.asyncio
async def test_typed_agent_turn_starts_correlates_and_maps_terminal_status() -> None:
    from manyselves.runtime.typed_agent_turn import TypedAgentTurn

    bus = MessageBus()
    bus_task = asyncio.create_task(bus.process_queue())

    class ScriptedTypedLoop:
        def __init__(self, runtime_id: str) -> None:
            self.runtime_id = runtime_id
            self.received: list[UserMessage] = []
            self.restored: list[dict[str, object]] = []
            self._callback = None

        def restore_conversation(
            self,
            messages,
            *,
            task_boundaries=(),
            handoff_summary=None,
        ) -> None:
            del task_boundaries, handoff_summary
            self.restored = list(messages)

        async def start(self) -> None:
            async def respond(message: UserMessage) -> None:
                if message.agent_type != self.runtime_id:
                    return
                self.received.append(message)
                if len(self.received) == 1:
                    await bus.publish(
                        AgentResultMessage(
                            sender=self.runtime_id,
                            workflow_id=message.workflow_id,
                            task_id="stale-task",
                            run_id=message.run_id,
                            result_path="stale.json",
                            task_attempt_id=message.task_attempt_id,
                            session_id=message.session_id,
                            status="completed",
                        )
                    )
                    await bus.publish(
                        AgentResultMessage(
                            sender=self.runtime_id,
                            workflow_id=message.workflow_id,
                            task_id=message.task_id,
                            run_id=message.run_id,
                            result_path="blocked.json",
                            task_attempt_id=message.task_attempt_id,
                            session_id=message.session_id,
                            status="blocked",
                        )
                    )
                    return
                await bus.publish(
                    Error(
                        source=self.runtime_id,
                        message="provider failed",
                        workflow_id=message.workflow_id,
                        run_id=message.run_id,
                        task_id=message.task_id,
                        task_attempt_id=message.task_attempt_id,
                        session_id=message.session_id,
                    )
                )

            self._callback = respond
            bus.subscribe(UserMessage, respond)

        async def stop(self) -> None:
            if self._callback is not None:
                bus.unsubscribe(UserMessage, self._callback)

        async def wait_until_turn_complete(self) -> None:
            return None

    loop = ScriptedTypedLoop("typed-runtime")
    service = AgentExecutionService(bus, timeout=1)
    turn = TypedAgentTurn(
        execution=service,
        workflow_id="workflow-typed",
        conversation_key="typed-conversation",
        runtime_id="typed-runtime",
        session_id="session-typed",
        session_factory=lambda: loop,
    )
    try:
        session = await turn.start_or_restore(
            restore=AgentSessionRestore(
                messages=[{"role": "user", "content": "restored"}]
            )
        )
        reused = await turn.start_or_restore()
        assert reused.loop is session.loop
        assert loop.restored == [{"role": "user", "content": "restored"}]

        first_request = AgentTurnRequest(
            content="typed task",
            message_id="typed-message-1",
            workflow_id="workflow-typed",
            run_id="run-typed",
            task_id="task-typed",
            task_attempt_id="attempt-typed",
        )
        first = await turn.dispatch(
            session,
            first_request,
            terminals=(
                turn.result_terminal(
                    run_id="run-typed",
                    task_id="task-typed",
                    task_attempt_id="attempt-typed",
                ),
            ),
        )
        blocked = turn.map_outcome(
            first,
            session_id=session.session_id,
            decode_result=lambda result_ref: result_ref,
        )

        second_request = AgentTurnRequest(
            content="typed task again",
            message_id="typed-message-2",
            workflow_id="workflow-typed",
            run_id="run-typed",
            task_id="task-typed-2",
            task_attempt_id="attempt-typed-2",
        )
        second = await turn.dispatch(
            session,
            second_request,
            terminals=(
                turn.result_terminal(
                    run_id="run-typed",
                    task_id="task-typed-2",
                    task_attempt_id="attempt-typed-2",
                ),
            ),
        )
        failed = turn.map_outcome(
            second,
            session_id=session.session_id,
            decode_result=lambda result_ref: result_ref,
        )
    finally:
        await service.close_workflow("workflow-typed")
        bus.shutdown()
        await bus_task

    assert blocked.status == "blocked"
    assert blocked.error == "blocked"
    assert failed.status == "failed"
    assert failed.error is not None
    assert "provider failed" in failed.error
    assert {message.session_id for message in loop.received} == {"session-typed"}
    assert len(loop.received) == 2

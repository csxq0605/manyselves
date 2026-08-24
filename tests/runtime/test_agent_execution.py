import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import pytest

from manyselves.interfaces.types import AgentResponse, AgentResultMessage, UserMessage
from manyselves.kernel.definitions import RecoveryPolicyDefinition, RecoveryRule
from manyselves.kernel.recovery import RecoveryActionKind, RecoveryEventKind
from manyselves.runtime.agent_execution import (
    AgentExecutionService,
    AgentRecoveryCompleted,
    AgentRecoveryDirective,
    AgentRecoveryExecutionError,
    AgentRecoveryProgress,
    AgentRecoveryRequired,
    AgentSessionRestore,
    AgentTerminalSubscription,
    AgentTurnRequest,
)
from manyselves.runtime.agent_recovery import AgentRecoveryDriver
from manyselves.runtime.loops.bus import MessageBus


@dataclass
class ScriptedAgentLoop:
    bus: MessageBus
    runtime_id: str
    responses: list[str]
    restored_messages: list[dict[str, Any]] = field(default_factory=list)
    started: int = 0
    stopped: int = 0
    completed_turns: int = 0
    received: list[UserMessage] = field(default_factory=list)
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
            self.received.append(message)
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
async def test_recovery_loop_corrects_in_same_session_until_typed_terminal() -> None:
    bus = MessageBus()
    bus_task = asyncio.create_task(bus.process_queue())
    loop = ScriptedAgentLoop(bus, "runtime-agent", ["natural answer"])
    service = AgentExecutionService(bus, timeout=1)
    session = await service.start_or_restore(
        workflow_id="workflow-1",
        conversation_key="agent-a",
        runtime_id="runtime-agent",
        session_id="provider-session-9",
        session_factory=lambda: loop,
    )

    async def publish_typed(message: UserMessage) -> None:
        if message.turn_kind != "submission_correction":
            return
        await bus.publish(
            AgentResultMessage(
                sender="runtime-agent",
                task_id=message.task_id,
                run_id=message.run_id,
                result_path="Work/results/corrected.json",
                workflow_id=message.workflow_id,
                task_attempt_id=message.task_attempt_id,
                session_id=message.session_id,
            )
        )

    bus.subscribe(UserMessage, publish_typed)
    initial = AgentTurnRequest(
        content="initial task",
        message_id="task-1",
        workflow_id="workflow-1",
        run_id="run-1",
        task_id="task-1",
        task_attempt_id="attempt-1",
    )

    async def interpret(outcome, request):
        if outcome.kind == "typed_result":
            return AgentRecoveryCompleted(result="accepted")
        assert request.turn_kind == "task_initial"
        return AgentRecoveryRequired(
            event_kind=RecoveryEventKind.NATURAL_LANGUAGE_WITHOUT_SUBMISSION,
            fallback_action=RecoveryActionKind.CORRECT,
            detail={"terminal": "natural"},
        )

    async def build_turn(
        directive: AgentRecoveryDirective,
        observation: AgentRecoveryRequired,
    ) -> AgentTurnRequest:
        del observation
        assert directive.action is RecoveryActionKind.CORRECT
        assert directive.prompt == "Use the declared submission tool."
        return AgentTurnRequest(
            **{
                **initial.__dict__,
                "content": directive.prompt,
                "internal": True,
                "turn_kind": "submission_correction",
            }
        )

    async def stop(_outcome, _directive):
        return "stopped"

    async def reuse_result(_outcome, _directive):
        return "reused"

    result = await service.execute_with_recovery(
        session,
        initial,
        recovery=AgentRecoveryDriver(
            RecoveryPolicyDefinition(
                id="correct-natural",
                version="1.0.0",
                description="Correct natural completion",
                rules={
                    "natural_language_without_submission": RecoveryRule(
                        action="correct",
                        prompt="Use the declared submission tool.",
                    )
                },
            )
        ),
        interpret=interpret,
        build_turn=build_turn,
        stop=stop,
        reuse_result=reuse_result,
        terminals=(
            AgentTerminalSubscription(
                kind="typed_result",
                message_type=AgentResultMessage,
                predicate=lambda item: item.result_path.endswith("corrected.json"),
            ),
        ),
    )

    assert result == "accepted"
    assert [item.turn_kind for item in loop.received] == [
        "task_initial",
        "submission_correction",
    ]
    assert {item.session_id for item in loop.received} == {"provider-session-9"}

    await service.close_workflow("workflow-1")
    bus.shutdown()
    await bus_task


@pytest.mark.asyncio
async def test_recovery_loop_fails_on_declared_fail_action() -> None:
    bus = MessageBus()
    bus_task = asyncio.create_task(bus.process_queue())
    loop = ScriptedAgentLoop(bus, "runtime-agent", ["natural"])
    service = AgentExecutionService(bus, timeout=1)
    session = await service.start_or_restore(
        workflow_id="workflow-1",
        conversation_key="agent-a",
        runtime_id="runtime-agent",
        session_id="provider-session-12",
        session_factory=lambda: loop,
    )

    async def interpret(_outcome, _request):
        return AgentRecoveryRequired(
            event_kind=RecoveryEventKind.NATURAL_LANGUAGE_WITHOUT_SUBMISSION,
            fallback_action=RecoveryActionKind.CORRECT,
        )

    async def build_turn(_directive, _observation):
        return AgentTurnRequest(
            content="unused",
            message_id="task-1",
            workflow_id="workflow-1",
            run_id="run-1",
            task_id="task-1",
            task_attempt_id="attempt-1",
        )

    async def stop(_outcome, _directive):
        return "stopped"

    async def reuse_result(_outcome, _directive):
        return "reused"

    with pytest.raises(AgentRecoveryExecutionError, match="declared fail"):
        await service.execute_with_recovery(
            session,
            AgentTurnRequest(
                content="initial task",
                message_id="task-1",
                workflow_id="workflow-1",
                run_id="run-1",
                task_id="task-1",
                task_attempt_id="attempt-1",
            ),
            recovery=AgentRecoveryDriver(
                RecoveryPolicyDefinition(
                    id="fail-natural",
                    version="1.0.0",
                    description="Fail natural completion",
                    rules={
                        "natural_language_without_submission": RecoveryRule(
                            action="fail"
                        )
                    },
                )
            ),
            interpret=interpret,
            build_turn=build_turn,
            stop=stop,
            reuse_result=reuse_result,
        )

    await service.close_workflow("workflow-1")
    bus.shutdown()
    await bus_task


@pytest.mark.asyncio
async def test_recovery_loop_owns_progress_observation_and_continuation() -> None:
    bus = MessageBus()
    bus_task = asyncio.create_task(bus.process_queue())
    loop = ScriptedAgentLoop(bus, "runtime-agent", ["boundary", "accepted"])
    service = AgentExecutionService(bus, timeout=1)
    session = await service.start_or_restore(
        workflow_id="workflow-1",
        conversation_key="agent-a",
        runtime_id="runtime-agent",
        session_id="provider-session-15",
        session_factory=lambda: loop,
    )
    initial = AgentTurnRequest(
        content="initial task",
        message_id="task-1",
        workflow_id="workflow-1",
        run_id="run-1",
        task_id="task-1",
        task_attempt_id="attempt-1",
    )
    progress_observations: list[bool] = []

    class ObservedRecoveryDriver(AgentRecoveryDriver):
        def observe_progress(self, *, progressed, detail=None):
            progress_observations.append(progressed)
            return super().observe_progress(
                progressed=progressed,
                detail=detail,
            )

    async def interpret(outcome, _request):
        response = outcome.message
        if response.content == "accepted":
            return AgentRecoveryCompleted(result="accepted")
        return AgentRecoveryProgress(
            kind="progressed",
            continuation=AgentRecoveryRequired(
                event_kind=RecoveryEventKind.TOOL_SLICE_BOUNDARY,
                fallback_action=RecoveryActionKind.CONTINUE,
            ),
            detail={"turn_kind": "tool_slice_continuation"},
        )

    async def build_turn(_directive, _observation):
        return AgentTurnRequest(
            **{
                **initial.__dict__,
                "content": "continue",
                "internal": True,
                "turn_kind": "tool_slice_continuation",
            }
        )

    async def stop(_outcome, _directive):
        return "stopped"

    async def reuse_result(_outcome, _directive):
        return "reused"

    result = await service.execute_with_recovery(
        session,
        initial,
        recovery=ObservedRecoveryDriver(
            RecoveryPolicyDefinition(
                id="continue-progress",
                version="1.0.0",
                description="Continue productive work",
                rules={
                    "tool_slice_boundary": RecoveryRule(action="continue"),
                    "no_progress": RecoveryRule(action="stop"),
                },
            )
        ),
        interpret=interpret,
        build_turn=build_turn,
        stop=stop,
        reuse_result=reuse_result,
    )

    assert result == "accepted"
    assert progress_observations == [True]
    assert [item.turn_kind for item in loop.received] == [
        "task_initial",
        "tool_slice_continuation",
    ]
    assert {item.session_id for item in loop.received} == {"provider-session-15"}

    await service.close_workflow("workflow-1")
    bus.shutdown()
    await bus_task


@pytest.mark.asyncio
async def test_recovery_loop_stops_no_progress_without_another_turn() -> None:
    bus = MessageBus()
    bus_task = asyncio.create_task(bus.process_queue())
    loop = ScriptedAgentLoop(bus, "runtime-agent", ["boundary"])
    service = AgentExecutionService(bus, timeout=1)
    session = await service.start_or_restore(
        workflow_id="workflow-1",
        conversation_key="agent-a",
        runtime_id="runtime-agent",
        session_id="provider-session-16",
        session_factory=lambda: loop,
    )
    initial = AgentTurnRequest(
        content="initial task",
        message_id="task-1",
        workflow_id="workflow-1",
        run_id="run-1",
        task_id="task-1",
        task_attempt_id="attempt-1",
    )

    async def interpret(_outcome, _request):
        return AgentRecoveryProgress(
            kind="no_progress",
            continuation=AgentRecoveryRequired(
                event_kind=RecoveryEventKind.TOOL_SLICE_BOUNDARY,
                fallback_action=RecoveryActionKind.CONTINUE,
            ),
            detail={"turn_kind": "tool_slice_continuation"},
        )

    async def build_turn(_directive, _observation):
        return initial

    async def stop(_outcome, directive):
        assert directive.event_kind is RecoveryEventKind.NO_PROGRESS
        return "stopped"

    async def reuse_result(_outcome, _directive):
        return "reused"

    result = await service.execute_with_recovery(
        session,
        initial,
        recovery=AgentRecoveryDriver(
            RecoveryPolicyDefinition(
                id="stop-no-progress",
                version="1.0.0",
                description="Stop stalled work",
                rules={"no_progress": RecoveryRule(action="stop")},
            )
        ),
        interpret=interpret,
        build_turn=build_turn,
        stop=stop,
        reuse_result=reuse_result,
    )

    assert result == "stopped"
    assert len(loop.received) == 1

    await service.close_workflow("workflow-1")
    bus.shutdown()
    await bus_task


@pytest.mark.asyncio
async def test_completed_result_reuse_does_not_create_a_session() -> None:
    service = AgentExecutionService(MessageBus())
    completed = object()
    actions: list[RecoveryActionKind] = []

    async def reuse_result(directive):
        actions.append(directive.action)
        return completed

    async def stop(_directive):
        return "stopped"

    recovered = await service.recover_completed_result(
        recovery=AgentRecoveryDriver(),
        detail={"task_id": "task-1", "source": "persisted_result"},
        reuse_result=reuse_result,
        stop=stop,
    )

    assert recovered is completed
    assert actions == [RecoveryActionKind.REUSE_RESULT]
    assert service.sessions == {}


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

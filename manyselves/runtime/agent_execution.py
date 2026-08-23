"""Business-neutral Agent session and single-turn execution lifecycle."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from manyselves.interfaces.types import AgentResponse, Error, Message, UserMessage
from manyselves.kernel.recovery import RecoveryActionKind, RecoveryEventKind

from .agent_recovery import AgentRecoveryDriver


class AgentSessionLoop(Protocol):
    """Minimum session lifecycle required by the generic Agent runtime."""

    def restore_conversation(
        self,
        messages: Sequence[Mapping[str, Any]],
        *,
        task_boundaries: Sequence[Mapping[str, Any]] = (),
        handoff_summary: Mapping[str, Any] | None = None,
    ) -> None: ...

    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    async def wait_until_turn_complete(self) -> None: ...


class AgentMessageBus(Protocol):
    """Message operations used by one generic Agent turn."""

    async def publish(self, message: Message) -> None: ...

    async def wait_for(
        self,
        message_type: type[Message],
        predicate: Callable[[Any], bool],
        timeout: float | None,
    ) -> Message: ...


@dataclass(frozen=True)
class AgentSessionRestore:
    """Conversation state restored before a newly-created loop starts."""

    messages: Sequence[Mapping[str, Any]]
    task_boundaries: Sequence[Mapping[str, Any]] = ()
    handoff_summary: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class AgentExecutionSession:
    """One live AgentLoop bound to a stable workflow conversation identity."""

    workflow_id: str
    conversation_key: str
    loop: AgentSessionLoop
    session_id: str
    runtime_id: str
    created: bool


@dataclass(frozen=True)
class AgentTurnRequest:
    """Generic correlation and content for one AgentLoop turn."""

    content: str
    message_id: str
    workflow_id: str
    run_id: str
    task_id: str
    task_attempt_id: str
    source: str = "workflow"
    internal: bool = False
    provider_stream_idle_timeout_seconds: float | None = None
    turn_kind: Literal[
        "task_initial",
        "tool_followup",
        "submission_correction",
        "tool_slice_continuation",
        "max_tokens_continuation",
        "guard",
    ] = "task_initial"


@dataclass(frozen=True)
class AgentTerminalSubscription:
    """Capability-supplied terminal message match, without result decoding."""

    kind: str
    message_type: type[Message]
    predicate: Callable[[Any], bool]


@dataclass(frozen=True)
class AgentTurnOutcome:
    """The first terminal message observed for a dispatched turn."""

    kind: str
    message: Message
    session_id: str
    runtime_id: str


@dataclass(frozen=True)
class AgentRecoveryCompleted:
    """A Capability decoded one terminal into its completed result."""

    result: Any


@dataclass(frozen=True)
class AgentRecoveryRequired:
    """A Capability classified one terminal as a recoverable event."""

    event_kind: RecoveryEventKind
    fallback_action: RecoveryActionKind
    detail: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class AgentRecoveryStopped:
    """A Capability classified one terminal as final without recovery."""

    reason: str = "terminal_stop"


@dataclass(frozen=True)
class AgentRecoveryProgress:
    """A typed progress observation preceding one recoverable boundary."""

    kind: Literal["progressed", "no_progress"]
    continuation: AgentRecoveryRequired
    detail: Mapping[str, Any]


AgentRecoveryObservation = (
    AgentRecoveryCompleted
    | AgentRecoveryRequired
    | AgentRecoveryStopped
    | AgentRecoveryProgress
)


@dataclass(frozen=True)
class AgentRecoveryDirective:
    """Runtime decision passed to a Capability message or terminal adapter."""

    event_kind: RecoveryEventKind
    action: RecoveryActionKind
    prompt: str | None = None
    reason: str | None = None
    detail: Mapping[str, Any] | None = None


class AgentRecoveryExecutionError(RuntimeError):
    """A declared recovery action cannot be executed for the observed event."""


class AgentExecutionService:
    """Own AgentLoop sessions and dispatch exactly one correlated turn at a time.

    A Capability supplies a factory for a fully configured ``AgentLoop`` and
    optional terminal-message predicates.  The service owns loop creation,
    restore-before-start, session reuse, bus dispatch/wait, and workflow close.
    It does not know prompts, tools, contracts, result models, or recovery
    policy semantics.
    """

    def __init__(self, bus: AgentMessageBus, *, timeout: float | None = None) -> None:
        self.bus = bus
        self.timeout = timeout
        self._sessions: dict[
            tuple[str, str], tuple[AgentSessionLoop, str, str]
        ] = {}

    @property
    def sessions(
        self,
    ) -> dict[tuple[str, str], tuple[AgentSessionLoop, str, str]]:
        """Expose the live registry for runtime inspection during migration."""

        return self._sessions

    def session(
        self,
        workflow_id: str,
        conversation_key: str,
    ) -> AgentExecutionSession | None:
        """Return the current live handle for one conversation identity."""

        cached = self._sessions.get((workflow_id, conversation_key))
        if cached is None:
            return None
        loop, session_id, runtime_id = cached
        return AgentExecutionSession(
            workflow_id=workflow_id,
            conversation_key=conversation_key,
            loop=loop,
            session_id=session_id,
            runtime_id=runtime_id,
            created=False,
        )

    async def start_or_restore(
        self,
        *,
        workflow_id: str,
        conversation_key: str,
        runtime_id: str,
        session_id: str,
        session_factory: Callable[[], AgentSessionLoop],
        restore: AgentSessionRestore | None = None,
    ) -> AgentExecutionSession:
        """Return a live stable session, creating/restoring it only once."""

        key = (workflow_id, conversation_key)
        cached = self._sessions.get(key)
        if cached is not None:
            loop, actual_session_id, actual_runtime_id = cached
            return AgentExecutionSession(
                workflow_id=workflow_id,
                conversation_key=conversation_key,
                loop=loop,
                session_id=actual_session_id,
                runtime_id=actual_runtime_id,
                created=False,
            )

        loop = session_factory()
        if restore is not None:
            loop.restore_conversation(
                restore.messages,
                task_boundaries=restore.task_boundaries,
                handoff_summary=restore.handoff_summary,
            )
        self._sessions[key] = (loop, session_id, runtime_id)
        try:
            await loop.start()
        except BaseException:
            self._sessions.pop(key, None)
            raise
        return AgentExecutionSession(
            workflow_id=workflow_id,
            conversation_key=conversation_key,
            loop=loop,
            session_id=session_id,
            runtime_id=runtime_id,
            created=True,
        )

    async def dispatch_turn(
        self,
        session: AgentExecutionSession,
        request: AgentTurnRequest,
        *,
        terminals: Sequence[AgentTerminalSubscription] = (),
    ) -> AgentTurnOutcome:
        """Publish one turn and wait for a custom, error, or natural terminal."""

        waiters: list[tuple[str, asyncio.Task[Message]]] = []
        for terminal in terminals:
            waiter = asyncio.create_task(
                self.bus.wait_for(
                    terminal.message_type,
                    terminal.predicate,
                    timeout=self.timeout,
                )
            )
            waiters.append((terminal.kind, waiter))

        error_waiter = asyncio.create_task(
            self.bus.wait_for(
                Error,
                lambda item: (
                    item.source == session.runtime_id
                    and item.workflow_id == request.workflow_id
                    and item.run_id == request.run_id
                    and item.task_id == request.task_id
                    and item.task_attempt_id == request.task_attempt_id
                    and item.session_id == session.session_id
                ),
                timeout=self.timeout,
            )
        )
        response_waiter = asyncio.create_task(
            self.bus.wait_for(
                AgentResponse,
                lambda item: (
                    item.agent_type == session.runtime_id
                    and item.message_id == request.message_id
                    and item.workflow_id == request.workflow_id
                    and item.run_id == request.run_id
                    and item.task_id == request.task_id
                    and item.task_attempt_id == request.task_attempt_id
                    and item.session_id == session.session_id
                    and not item.streaming
                ),
                timeout=self.timeout,
            )
        )
        waiters.extend((("error", error_waiter), ("response", response_waiter)))

        try:
            await asyncio.sleep(0)
            await self.bus.publish(
                UserMessage(
                    agent_type=session.runtime_id,
                    source=request.source,
                    message_id=request.message_id,
                    content=request.content,
                    internal=request.internal,
                    provider_stream_idle_timeout_seconds=(
                        request.provider_stream_idle_timeout_seconds
                    ),
                    workflow_id=request.workflow_id,
                    run_id=request.run_id,
                    task_id=request.task_id,
                    task_attempt_id=request.task_attempt_id,
                    session_id=session.session_id,
                    turn_kind=request.turn_kind,
                )
            )
            done, _pending = await asyncio.wait(
                [waiter for _kind, waiter in waiters],
                return_when=asyncio.FIRST_COMPLETED,
            )
            kind, waiter = next(item for item in waiters if item[1] in done)
            return AgentTurnOutcome(
                kind=kind,
                message=waiter.result(),
                session_id=session.session_id,
                runtime_id=session.runtime_id,
            )
        finally:
            for _kind, waiter in waiters:
                if not waiter.done():
                    waiter.cancel()
            await asyncio.gather(
                *(waiter for _kind, waiter in waiters),
                return_exceptions=True,
            )

    async def execute_with_recovery(
        self,
        session: AgentExecutionSession,
        initial: AgentTurnRequest,
        *,
        recovery: AgentRecoveryDriver,
        interpret: Callable[
            [AgentTurnOutcome, AgentTurnRequest],
            Coroutine[Any, Any, AgentRecoveryObservation],
        ],
        build_turn: Callable[
            [AgentRecoveryDirective, AgentRecoveryRequired],
            Coroutine[Any, Any, AgentTurnRequest],
        ],
        stop: Callable[
            [AgentTurnOutcome, AgentRecoveryDirective],
            Coroutine[Any, Any, Any],
        ],
        reuse_result: Callable[
            [AgentTurnOutcome, AgentRecoveryDirective],
            Coroutine[Any, Any, Any],
        ],
        terminals: Sequence[AgentTerminalSubscription] = (),
        observed_outcome: AgentTurnOutcome | None = None,
    ) -> Any:
        """Drive interpreted terminal events through recovery on one session.

        All Capability ports are async and narrow: classify/decode a terminal,
        build the next domain message, persist a stop, or load a reusable result.
        """

        request = initial
        while True:
            if observed_outcome is None:
                outcome = await self.dispatch_turn(
                    session,
                    request,
                    terminals=terminals,
                )
            else:
                outcome = observed_outcome
                observed_outcome = None
            observation = await interpret(outcome, request)
            if isinstance(observation, AgentRecoveryCompleted):
                return observation.result
            if isinstance(observation, AgentRecoveryStopped):
                directive = AgentRecoveryDirective(
                    event_kind=RecoveryEventKind.NATURAL_LANGUAGE_WITHOUT_SUBMISSION,
                    action=RecoveryActionKind.STOP,
                    reason=observation.reason,
                )
                return await stop(outcome, directive)

            recovery_request = observation
            if isinstance(observation, AgentRecoveryProgress):
                recovery_request = observation.continuation
                if observation.kind == "progressed":
                    recovery.observe_progress(
                        progressed=True,
                        detail=observation.detail,
                    )
                    decision = recovery.decide(
                        recovery_request.event_kind,
                        recovery_request.detail,
                    )
                    directive_event_kind = recovery_request.event_kind
                    fallback_action = recovery_request.fallback_action
                    directive_detail = recovery_request.detail
                else:
                    decision = recovery.observe_progress(
                        progressed=False,
                        detail=observation.detail,
                    )
                    directive_event_kind = RecoveryEventKind.NO_PROGRESS
                    fallback_action = RecoveryActionKind.STOP
                    directive_detail = observation.detail
            else:
                decision = recovery.decide(
                    recovery_request.event_kind,
                    recovery_request.detail,
                )
                directive_event_kind = recovery_request.event_kind
                fallback_action = recovery_request.fallback_action
                directive_detail = recovery_request.detail
            directive = AgentRecoveryDirective(
                event_kind=directive_event_kind,
                action=(
                    fallback_action
                    if decision is None
                    else decision.action
                ),
                prompt=None if decision is None else decision.prompt,
                reason=None if decision is None else decision.reason,
                detail=directive_detail,
            )
            if directive.action is RecoveryActionKind.STOP:
                return await stop(outcome, directive)
            if directive.action is RecoveryActionKind.FAIL:
                raise AgentRecoveryExecutionError(
                    f"recovery policy declared fail for "
                    f"{directive.event_kind.value}"
                )
            if directive.action is RecoveryActionKind.REUSE_RESULT:
                return await reuse_result(outcome, directive)
            request = await build_turn(directive, recovery_request)

    async def wait_until_turn_complete(
        self,
        session: AgentExecutionSession,
    ) -> None:
        """Wait until the loop has finalized Provider and Tool work for a turn."""

        await session.loop.wait_until_turn_complete()

    async def close_workflow(self, workflow_id: str) -> None:
        """Stop and forget every live session owned by one workflow."""

        keys = [key for key in self._sessions if key[0] == workflow_id]
        for key in keys:
            loop, _session_id, _runtime_id = self._sessions.pop(key)
            await loop.stop()

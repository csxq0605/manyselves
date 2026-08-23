"""Business-neutral Agent session and single-turn execution lifecycle."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol

from manyselves.interfaces.types import AgentResponse, Error, Message, UserMessage


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
            for kind, waiter in waiters:
                if waiter in done:
                    return AgentTurnOutcome(
                        kind=kind,
                        message=waiter.result(),
                        session_id=session.session_id,
                        runtime_id=session.runtime_id,
                    )
            raise RuntimeError("Agent turn ended without a terminal message")
        finally:
            for _kind, waiter in waiters:
                if not waiter.done():
                    waiter.cancel()
            await asyncio.gather(
                *(waiter for _kind, waiter in waiters),
                return_exceptions=True,
            )

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

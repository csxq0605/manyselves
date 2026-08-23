"""Business-neutral typed Agent turn lifecycle primitives."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Literal

from manyselves.interfaces.types import AgentResponse, AgentResultMessage
from manyselves.kernel.ports import AgentInvocationOutcome

from .agent_execution import (
    AgentExecutionService,
    AgentExecutionSession,
    AgentSessionLoop,
    AgentSessionRestore,
    AgentTerminalSubscription,
    AgentTurnOutcome,
    AgentTurnRequest,
)

TypedResultDecoder = Callable[[str], Any]
TypedAgentResultStatus = Literal["completed", "blocked", "incomplete", "failed"]


@dataclass(frozen=True)
class TypedAgentTurn:
    """Own one stable Agent session and correlate typed result terminals.

    The primitive deliberately does not know a Capability input, prompt,
    result model, tool contract, or recovery policy.  Callers supply those
    pieces and retain terminal interpretation/recovery decisions.
    """

    execution: AgentExecutionService
    workflow_id: str
    conversation_key: str
    runtime_id: str
    session_id: str
    session_factory: Callable[[], AgentSessionLoop]

    async def start_or_restore(
        self,
        *,
        restore: AgentSessionRestore | None = None,
    ) -> AgentExecutionSession:
        """Start one session or return the existing session for this identity."""

        return await self.execution.start_or_restore(
            workflow_id=self.workflow_id,
            conversation_key=self.conversation_key,
            runtime_id=self.runtime_id,
            session_id=self.session_id,
            session_factory=self.session_factory,
            restore=restore,
        )

    def result_terminal(
        self,
        *,
        run_id: str,
        task_id: str,
        task_attempt_id: str,
        session_id: str | None = None,
    ) -> AgentTerminalSubscription:
        """Build the exact typed-result correlation for one dispatched turn."""

        expected_session_id = session_id or self.session_id

        def matches(item: AgentResultMessage) -> bool:
            return (
                item.sender == self.runtime_id
                and item.workflow_id == self.workflow_id
                and item.run_id == run_id
                and item.task_id == task_id
                and item.task_attempt_id == task_attempt_id
                and item.session_id == expected_session_id
            )

        return AgentTerminalSubscription(
            kind="typed_result",
            message_type=AgentResultMessage,
            predicate=matches,
        )

    async def dispatch(
        self,
        session: AgentExecutionSession,
        request: AgentTurnRequest,
        *,
        terminals: Sequence[AgentTerminalSubscription] = (),
    ) -> AgentTurnOutcome:
        """Dispatch one correlated turn through the neutral execution service."""

        return await self.execution.dispatch_turn(
            session,
            request,
            terminals=terminals,
        )

    @staticmethod
    def map_outcome(
        outcome: AgentTurnOutcome,
        *,
        session_id: str,
        decode_result: TypedResultDecoder,
    ) -> AgentInvocationOutcome:
        """Map generic terminal/status shapes to the neutral invocation port."""

        if outcome.kind == "typed_result":
            message = outcome.message
            if not isinstance(message, AgentResultMessage):
                return AgentInvocationOutcome(
                    status="failed",
                    session_id=session_id,
                    error="typed terminal was not an AgentResultMessage",
                )
            if message.status != "completed":
                status: TypedAgentResultStatus = (
                    "blocked" if message.status == "blocked" else message.status
                )
                return AgentInvocationOutcome(
                    status=status,
                    session_id=session_id,
                    error=message.status,
                )
            try:
                result = decode_result(message.result_path)
            except (OSError, TypeError, ValueError) as exc:
                return AgentInvocationOutcome(
                    status="failed",
                    session_id=session_id,
                    error=str(exc),
                )
            return AgentInvocationOutcome(
                status="ok",
                result=result,
                session_id=session_id,
            )
        if outcome.kind == "error":
            message = outcome.message
            return AgentInvocationOutcome(
                status="failed",
                session_id=session_id,
                error=str(message),
            )
        if outcome.kind == "response" and isinstance(outcome.message, AgentResponse):
            return AgentInvocationOutcome(
                status="incomplete",
                session_id=session_id,
                error="Agent turn ended without a typed result",
            )
        return AgentInvocationOutcome(
            status="failed",
            session_id=session_id,
            error="Agent turn returned an unknown terminal",
        )


__all__ = ["TypedAgentTurn", "TypedResultDecoder"]

"""Capability-owned recovery adapter for typed Reporting Agent turns.

The generic runtime owns the recovery loop.  This adapter only translates the
existing AgentLoop response markers into the neutral recovery vocabulary and
lets the Capability provide the next-turn prompt and result decoder.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from manyselves.core.loops.agent_loop import (
    AGENT_MAX_TOKENS_CONTINUATION_REQUIRED,
    AGENT_TURN_CONTINUATION_REQUIRED,
)
from manyselves.interfaces.types import AgentResponse, AgentResultMessage
from manyselves.kernel.definitions import RecoveryPolicyDefinition
from manyselves.kernel.ports import AgentInvocationOutcome
from manyselves.kernel.recovery import RecoveryActionKind, RecoveryEventKind
from manyselves.runtime.agent_execution import (
    AgentExecutionService,
    AgentExecutionSession,
    AgentRecoveryCompleted,
    AgentRecoveryDirective,
    AgentRecoveryObservation,
    AgentRecoveryRequired,
    AgentRecoveryStopped,
    AgentTerminalSubscription,
    AgentTurnOutcome,
    AgentTurnRequest,
)
from manyselves.runtime.agent_recovery import AgentRecoveryDriver

PromptBuilder = Callable[[RecoveryEventKind], str]
ResultDecoder = Callable[[str], Any]


async def execute_reporting_recovery(
    execution: AgentExecutionService,
    session: AgentExecutionSession,
    initial: AgentTurnRequest,
    *,
    recovery_policy: RecoveryPolicyDefinition,
    terminals: Sequence[AgentTerminalSubscription],
    prompt_builder: PromptBuilder,
    result_decoder: ResultDecoder,
) -> Any:
    """Run declared Reporting recovery on the existing Agent session.

    ``AgentExecutionService`` remains the owner of dispatch, correlation, and
    loop progression.  The Capability owns only the marker interpretation,
    prompt text, and typed payload decoding supplied through this function.
    """

    async def interpret(
        outcome: AgentTurnOutcome,
        _request: AgentTurnRequest,
    ) -> AgentRecoveryObservation:
        if outcome.kind == "typed_result":
            message = outcome.message
            if not isinstance(message, AgentResultMessage):
                return AgentRecoveryStopped(
                    reason="typed terminal was not an AgentResultMessage"
                )
            if message.status != "completed":
                return AgentRecoveryStopped(reason=message.status)
            try:
                return AgentRecoveryCompleted(
                    result=result_decoder(message.result_path)
                )
            except (OSError, TypeError, ValueError):
                return AgentRecoveryStopped(reason="typed result could not be decoded")
        if outcome.kind == "error":
            return AgentRecoveryStopped(reason=str(outcome.message))
        if isinstance(outcome.message, AgentResponse):
            if outcome.message.content == AGENT_MAX_TOKENS_CONTINUATION_REQUIRED:
                return AgentRecoveryRequired(
                    event_kind=RecoveryEventKind.MAX_TOKENS,
                    fallback_action=RecoveryActionKind.CONTINUE,
                    detail={"task_id": initial.task_id},
                )
            if outcome.message.content == AGENT_TURN_CONTINUATION_REQUIRED:
                return AgentRecoveryRequired(
                    event_kind=RecoveryEventKind.TOOL_SLICE_BOUNDARY,
                    fallback_action=RecoveryActionKind.CONTINUE,
                    detail={"task_id": initial.task_id},
                )
            return AgentRecoveryRequired(
                event_kind=RecoveryEventKind.NATURAL_LANGUAGE_WITHOUT_SUBMISSION,
                fallback_action=RecoveryActionKind.CORRECT,
                detail={"task_id": initial.task_id},
            )
        return AgentRecoveryStopped(reason="Agent turn returned an unknown terminal")

    async def build_turn(
        directive: AgentRecoveryDirective,
        _observation: AgentRecoveryRequired,
    ) -> AgentTurnRequest:
        event_kind = directive.event_kind
        suffix, turn_kind = {
            RecoveryEventKind.MAX_TOKENS: (
                "max-tokens-continuation",
                "max_tokens_continuation",
            ),
            RecoveryEventKind.TOOL_SLICE_BOUNDARY: (
                "tool-slice-continuation",
                "tool_slice_continuation",
            ),
            RecoveryEventKind.NATURAL_LANGUAGE_WITHOUT_SUBMISSION: (
                "correction",
                "submission_correction",
            ),
        }.get(
            event_kind,
            ("recovery", "submission_correction"),
        )
        return AgentTurnRequest(
            content=directive.prompt or prompt_builder(event_kind),
            message_id=f"{initial.task_id}:{initial.run_id}:{suffix}",
            workflow_id=initial.workflow_id,
            run_id=initial.run_id,
            task_id=initial.task_id,
            task_attempt_id=initial.task_attempt_id,
            internal=True,
            turn_kind=turn_kind,
        )

    async def stop(
        _outcome: AgentTurnOutcome,
        directive: AgentRecoveryDirective,
    ) -> AgentInvocationOutcome:
        return AgentInvocationOutcome(
            status="incomplete",
            session_id=session.session_id,
            error=directive.reason or "Agent turn ended without a typed result",
        )

    async def reuse_result(
        outcome: AgentTurnOutcome,
        _directive: AgentRecoveryDirective,
    ) -> Any:
        if not isinstance(outcome.message, AgentResultMessage):
            raise ValueError("reusable terminal was not an AgentResultMessage")
        return result_decoder(outcome.message.result_path)

    return await execution.execute_with_recovery(
        session,
        initial,
        recovery=AgentRecoveryDriver(recovery_policy),
        interpret=interpret,
        build_turn=build_turn,
        stop=stop,
        reuse_result=reuse_result,
        terminals=terminals,
    )


__all__ = ["execute_reporting_recovery"]

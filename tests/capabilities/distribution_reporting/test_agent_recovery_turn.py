"""Characterize Capability progress observation at Agent continuation boundaries."""

from __future__ import annotations

from typing import Any

import pytest

from manyselves.capabilities.distribution_reporting.runtime.agent_recovery_turn import (
    execute_reporting_recovery,
)
from manyselves.interfaces.types import AgentResponse
from manyselves.kernel.definitions import RecoveryPolicyDefinition, RecoveryRule
from manyselves.kernel.recovery import RecoveryEventKind
from manyselves.runtime.agent_execution import (
    AgentExecutionSession,
    AgentRecoveryProgress,
    AgentTurnOutcome,
    AgentTurnRequest,
)


@pytest.mark.asyncio
async def test_reporting_recovery_observes_progress_before_tool_slice_decision() -> None:
    """The Capability must classify progress before Runtime decides continuation."""

    loop = object()
    session = AgentExecutionSession(
        workflow_id="workflow",
        conversation_key="conversation",
        loop=loop,  # type: ignore[arg-type]
        session_id="session",
        runtime_id="runtime",
        created=False,
    )
    request = AgentTurnRequest(
        content="initial",
        message_id="message",
        workflow_id="workflow",
        run_id="run",
        task_id="task",
        task_attempt_id="attempt",
    )
    observed: list[tuple[object, RecoveryEventKind, dict[str, Any]]] = []

    async def observe_progress(
        current_loop: object,
        event_kind: RecoveryEventKind,
        detail: dict[str, Any],
    ) -> str:
        observed.append((current_loop, event_kind, detail))
        return "no_progress"

    class CapturingExecution:
        async def execute_with_recovery(self, _session, _initial, **ports):
            observation = await ports["interpret"](
                AgentTurnOutcome(
                    kind="response",
                    message=AgentResponse(
                        agent_type="runtime",
                        message_id="message",
                        content="AGENT_TURN_CONTINUATION_REQUIRED",
                        workflow_id="workflow",
                        run_id="run",
                        task_id="task",
                        task_attempt_id="attempt",
                        session_id="session",
                    ),
                    session_id="session",
                    runtime_id="runtime",
                ),
                request,
            )
            assert isinstance(observation, AgentRecoveryProgress)
            assert observation.kind == "no_progress"
            return observation

    result = await execute_reporting_recovery(
        CapturingExecution(),  # type: ignore[arg-type]
        session,
        request,
        recovery_policy=RecoveryPolicyDefinition(
            id="recovery",
            version="1.0.0",
            description="stop repeated no progress",
            rules={
                "tool_slice_boundary": RecoveryRule(action="continue"),
                "no_progress": RecoveryRule(action="stop"),
            },
        ),
        terminals=(),
        prompt_builder=lambda _event: "continue",
        result_decoder=lambda _ref: None,
        progress_observer=observe_progress,
    )

    assert isinstance(result, AgentRecoveryProgress)
    assert observed == [
        (
            loop,
            RecoveryEventKind.TOOL_SLICE_BOUNDARY,
            {"task_id": "task"},
        )
    ]

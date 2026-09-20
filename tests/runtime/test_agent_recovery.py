from __future__ import annotations

import pytest

from manyselves.kernel.definitions import RecoveryPolicyDefinition, RecoveryRule
from manyselves.kernel.recovery import RecoveryActionKind, RecoveryEventKind
from manyselves.runtime.agent_recovery import AgentRecoveryDriver


def _policy() -> RecoveryPolicyDefinition:
    return RecoveryPolicyDefinition(
        id="neutral-agent-recovery",
        version="1.0.0",
        description="Neutral Agent recovery choices",
        rules={
            "invalid_structured_output": RecoveryRule(
                action="correct",
                prompt="Correct the declared validation errors.",
                max_attempts=2,
            ),
            "max_tokens": RecoveryRule(
                action="continue",
                prompt="Continue in the same conversation.",
                max_attempts=3,
            ),
            "no_progress": RecoveryRule(action="stop"),
            "completed_tool_result": RecoveryRule(action="reuse_result"),
            "provider_error": RecoveryRule(action="continue", max_attempts=1),
        },
    )


def test_driver_is_inert_without_a_declared_policy() -> None:
    driver = AgentRecoveryDriver()

    assert driver.decide(RecoveryEventKind.MAX_TOKENS) is None
    assert driver.observe_progress(progressed=False) is None
    assert driver.snapshot_attempts() == {}


def test_driver_interprets_events_and_progress_without_capability_knowledge() -> None:
    driver = AgentRecoveryDriver(_policy())

    correction = driver.decide(
        RecoveryEventKind.INVALID_STRUCTURED_OUTPUT,
        {"errors": [{"field": "value", "problem": "required"}]},
    )
    progressed = driver.observe_progress(progressed=True)
    stalled = driver.observe_progress(
        progressed=False,
        detail={"source": "scripted-agent"},
    )

    assert correction is not None
    assert correction.action is RecoveryActionKind.CORRECT
    assert correction.prompt == "Correct the declared validation errors."
    assert correction.event.detail["errors"][0]["field"] == "value"
    assert progressed is None
    assert stalled is not None
    assert stalled.action is RecoveryActionKind.STOP
    assert stalled.event.detail == {"source": "scripted-agent"}


@pytest.mark.asyncio
async def test_provider_decision_uses_the_same_driver_state() -> None:
    driver = AgentRecoveryDriver(_policy())

    first = await driver.provider_decision({"error_type": "timeout"})
    second = await driver.provider_decision({"error_type": "timeout"})

    assert first == RecoveryActionKind.CONTINUE.value
    assert second == RecoveryActionKind.STOP.value
    assert driver.snapshot_attempts() == {"provider_error": 2}


def test_attempt_snapshot_round_trips_across_driver_instances() -> None:
    first = AgentRecoveryDriver(_policy())
    first.decide(RecoveryEventKind.MAX_TOKENS)
    first.decide(RecoveryEventKind.MAX_TOKENS)
    snapshot = first.snapshot_attempts()

    restored = AgentRecoveryDriver(_policy())

    assert restored.restore_attempts(snapshot) is True
    decision = restored.decide(RecoveryEventKind.MAX_TOKENS)
    assert decision is not None
    assert decision.attempt == 3
    assert restored.snapshot_attempts() == {"max_tokens": 3}


def test_invalid_attempt_snapshot_is_rejected_without_replacing_current_state() -> None:
    driver = AgentRecoveryDriver(_policy())
    driver.decide(RecoveryEventKind.MAX_TOKENS)

    assert driver.restore_attempts({"unknown_event": 4}) is False
    assert driver.snapshot_attempts() == {"max_tokens": 1}

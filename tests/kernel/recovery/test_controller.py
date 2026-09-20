import pytest

from manyselves.kernel.definitions import RecoveryPolicyDefinition, RecoveryRule
from manyselves.kernel.recovery import (
    ProgressObservation,
    RecoveryActionKind,
    RecoveryController,
    RecoveryEvent,
    RecoveryEventKind,
    RecoveryPolicyError,
    RecoveryState,
)


def _policy() -> RecoveryPolicyDefinition:
    return RecoveryPolicyDefinition(
        id="neutral-agent-recovery",
        version="1.0.0",
        description="Neutral recovery choices",
        rules={
            "natural_language_without_submission": RecoveryRule(
                action="correct",
                prompt="Submit the declared structured result.",
                max_attempts=1,
            ),
            "invalid_structured_output": RecoveryRule(
                action="correct",
                prompt="Correct only the declared validation errors.",
                max_attempts=2,
            ),
            "max_tokens": RecoveryRule(
                action="continue",
                prompt="Continue in the same conversation.",
                max_attempts=3,
            ),
            "tool_slice_boundary": RecoveryRule(
                action="continue",
                prompt="Continue unfinished tool work.",
            ),
            "tool_contract_error": RecoveryRule(
                action="correct",
                prompt="Correct the tool arguments against its contract.",
                max_attempts=2,
            ),
            "no_progress": RecoveryRule(action="stop"),
            "completed_tool_result": RecoveryRule(action="reuse_result"),
        },
    )


@pytest.mark.parametrize(
    ("event_kind", "expected_action"),
    [
        (
            RecoveryEventKind.NATURAL_LANGUAGE_WITHOUT_SUBMISSION,
            RecoveryActionKind.CORRECT,
        ),
        (RecoveryEventKind.INVALID_STRUCTURED_OUTPUT, RecoveryActionKind.CORRECT),
        (RecoveryEventKind.MAX_TOKENS, RecoveryActionKind.CONTINUE),
        (RecoveryEventKind.TOOL_SLICE_BOUNDARY, RecoveryActionKind.CONTINUE),
        (RecoveryEventKind.NO_PROGRESS, RecoveryActionKind.STOP),
        (RecoveryEventKind.TOOL_CONTRACT_ERROR, RecoveryActionKind.CORRECT),
        (RecoveryEventKind.COMPLETED_TOOL_RESULT, RecoveryActionKind.REUSE_RESULT),
    ],
)
def test_declared_recovery_events_map_to_capability_actions(
    event_kind: RecoveryEventKind,
    expected_action: RecoveryActionKind,
) -> None:
    state = RecoveryState()

    decision = RecoveryController().decide(
        RecoveryEvent(kind=event_kind),
        _policy(),
        state,
    )

    assert decision.action is expected_action
    assert decision.attempt == 1
    assert state.attempts[event_kind] == 1


def test_capability_supplies_correction_prompt_without_kernel_business_text() -> None:
    decision = RecoveryController().decide(
        RecoveryEvent(
            kind="invalid_structured_output",
            detail={"errors": [{"field": "value", "problem": "required"}]},
        ),
        _policy(),
        RecoveryState(),
    )

    assert decision.prompt == "Correct only the declared validation errors."
    assert decision.event.detail["errors"][0]["field"] == "value"


def test_declared_attempt_limit_stops_without_an_implicit_default() -> None:
    controller = RecoveryController()
    state = RecoveryState()
    policy = _policy()
    event = RecoveryEvent(kind="natural_language_without_submission")

    first = controller.decide(event, policy, state)
    second = controller.decide(event, policy, state)

    assert first.action is RecoveryActionKind.CORRECT
    assert second.action is RecoveryActionKind.STOP
    assert second.reason == "declared_attempt_limit_reached"
    assert second.attempt == 2


def test_rule_without_max_attempts_has_no_controller_imposed_limit() -> None:
    controller = RecoveryController()
    state = RecoveryState()
    policy = _policy()

    decisions = [
        controller.decide(RecoveryEvent(kind="tool_slice_boundary"), policy, state)
        for _ in range(10)
    ]

    assert all(item.action is RecoveryActionKind.CONTINUE for item in decisions)
    assert decisions[-1].attempt == 10


def test_no_progress_uses_adapter_observation_without_hashing() -> None:
    controller = RecoveryController()
    state = RecoveryState()

    progressed = controller.observe_progress(
        ProgressObservation(progressed=True),
        _policy(),
        state,
    )
    stalled = controller.observe_progress(
        ProgressObservation(progressed=False, detail={"source": "scripted-agent"}),
        _policy(),
        state,
    )

    assert progressed is None
    assert stalled is not None
    assert stalled.action is RecoveryActionKind.STOP
    assert stalled.event.detail == {"source": "scripted-agent"}


def test_missing_rule_is_reported_instead_of_inventing_recovery_behavior() -> None:
    with pytest.raises(RecoveryPolicyError, match="no declared recovery rule"):
        RecoveryController().decide(
            RecoveryEvent(kind="provider_error"),
            _policy(),
            RecoveryState(),
        )

"""Interpret only the recovery choices declared by a capability."""

from manyselves.kernel.definitions import RecoveryPolicyDefinition

from .models import (
    ProgressObservation,
    RecoveryActionKind,
    RecoveryDecision,
    RecoveryEvent,
    RecoveryEventKind,
    RecoveryState,
)


class RecoveryPolicyError(ValueError):
    """Raised when a capability does not declare the requested recovery behavior."""


class RecoveryController:
    """Map generic events to explicit policy rules without implicit defaults."""

    def decide(
        self,
        event: RecoveryEvent,
        policy: RecoveryPolicyDefinition,
        state: RecoveryState,
    ) -> RecoveryDecision:
        rule = policy.rules.get(event.kind.value)
        if rule is None:
            raise RecoveryPolicyError(
                f"no declared recovery rule for {event.kind.value} in {policy.id}"
            )
        try:
            declared_action = RecoveryActionKind(rule.action)
        except ValueError as exc:
            raise RecoveryPolicyError(
                f"unknown recovery action {rule.action} in {policy.id}"
            ) from exc
        attempt = state.attempts.get(event.kind, 0) + 1
        state.attempts[event.kind] = attempt
        if rule.max_attempts is not None and attempt > rule.max_attempts:
            return RecoveryDecision(
                event=event,
                action=RecoveryActionKind.STOP,
                attempt=attempt,
                reason="declared_attempt_limit_reached",
            )
        return RecoveryDecision(
            event=event,
            action=declared_action,
            attempt=attempt,
            prompt=rule.prompt,
        )

    def observe_progress(
        self,
        observation: ProgressObservation,
        policy: RecoveryPolicyDefinition,
        state: RecoveryState,
    ) -> RecoveryDecision | None:
        if observation.progressed:
            return None
        return self.decide(
            RecoveryEvent(
                kind=RecoveryEventKind.NO_PROGRESS,
                detail=observation.detail,
            ),
            policy,
            state,
        )

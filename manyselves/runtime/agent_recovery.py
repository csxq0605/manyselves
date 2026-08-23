"""Business-neutral recovery decisions for one Agent execution identity."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from manyselves.kernel.definitions import RecoveryPolicyDefinition
from manyselves.kernel.recovery import (
    ProgressObservation,
    RecoveryController,
    RecoveryDecision,
    RecoveryEvent,
    RecoveryEventKind,
    RecoveryState,
)


class AgentRecoveryDriver:
    """Apply one declared policy and carry its attempt state across dispatches.

    The driver knows only the Kernel recovery vocabulary. Capability adapters
    decide which events to report and how to execute the returned action or
    prompt.
    """

    def __init__(
        self,
        policy: RecoveryPolicyDefinition | None = None,
        *,
        controller: RecoveryController | None = None,
    ) -> None:
        self.policy = policy
        self._controller = controller or RecoveryController()
        self._state = RecoveryState()

    @property
    def enabled(self) -> bool:
        return self.policy is not None

    def decide(
        self,
        event_kind: RecoveryEventKind | str,
        detail: Mapping[str, Any] | None = None,
    ) -> RecoveryDecision | None:
        """Return the declared action for one generic Agent recovery event."""

        if self.policy is None:
            return None
        return self._controller.decide(
            RecoveryEvent(kind=event_kind, detail=dict(detail or {})),
            self.policy,
            self._state,
        )

    def observe_progress(
        self,
        *,
        progressed: bool,
        detail: Mapping[str, Any] | None = None,
    ) -> RecoveryDecision | None:
        """Interpret an adapter-provided progress observation."""

        if self.policy is None:
            return None
        return self._controller.observe_progress(
            ProgressObservation(
                progressed=progressed,
                detail=dict(detail or {}),
            ),
            self.policy,
            self._state,
        )

    async def provider_decision(self, detail: Mapping[str, Any]) -> str | None:
        """Return the declared action value expected by a Provider loop hook."""

        decision = self.decide(RecoveryEventKind.PROVIDER_ERROR, detail)
        return None if decision is None else decision.action.value

    def snapshot_attempts(self) -> dict[str, int]:
        """Return JSON-ready attempt state for the identity persistence layer."""

        return {
            kind.value: attempt
            for kind, attempt in self._state.attempts.items()
        }

    def restore_attempts(self, attempts: Mapping[str, Any]) -> bool:
        """Restore a persisted attempt snapshot without partially mutating state."""

        try:
            restored = RecoveryState.model_validate({"attempts": dict(attempts)})
        except (TypeError, ValueError):
            return False
        self._state.attempts = dict(restored.attempts)
        return True

"""Deterministic single-controller leases for runtime mutations."""

import secrets
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone

Clock = Callable[[], datetime]


class ControlLeaseError(RuntimeError):
    """Base class for stable control-lease failures."""

    code: str


class ControlLeaseHeld(ControlLeaseError):  # noqa: N818 - locked application error name
    """Another client currently owns the unexpired controller lease."""

    code = "CONTROL_LEASE_HELD"

    def __init__(self, controller_client_id: str) -> None:
        super().__init__(f"Runtime control is held by client {controller_client_id}")
        self.controller_client_id = controller_client_id


class ControlLeaseRequired(ControlLeaseError):  # noqa: N818 - locked application error name
    """A valid matching controller token is required."""

    code = "CONTROL_LEASE_REQUIRED"

    def __init__(self) -> None:
        super().__init__("A valid runtime control lease is required")


@dataclass(frozen=True, slots=True)
class ControlLease:
    """One controller lease without any logging or serialization behavior."""

    client_id: str
    actor_id: str
    token: str
    expires_at: datetime


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class ControlLeaseService:
    """Maintain at most one unexpired controller for a runtime process."""

    def __init__(
        self,
        *,
        clock: Clock = _utc_now,
        ttl: timedelta = timedelta(seconds=30),
    ) -> None:
        if ttl <= timedelta(0):
            raise ValueError("Control lease TTL must be positive")
        self._clock = clock
        self._ttl = ttl
        self._current: ControlLease | None = None

    @property
    def current_controller_client_id(self) -> str | None:
        """Return the non-expired controller client without exposing its token."""
        current = self._unexpired_current()
        return None if current is None else current.client_id

    def acquire(self, *, client_id: str, actor_id: str) -> ControlLease:
        """Acquire, or renew idempotently for the same controller client."""
        current = self._unexpired_current()
        now = self._clock()
        if current is not None:
            if current.client_id != client_id:
                raise ControlLeaseHeld(current.client_id)
            renewed = replace(current, expires_at=now + self._ttl)
            self._current = renewed
            return renewed

        lease = ControlLease(
            client_id=client_id,
            actor_id=actor_id,
            token=secrets.token_urlsafe(32),
            expires_at=now + self._ttl,
        )
        self._current = lease
        return lease

    def require(self, token: str) -> ControlLease:
        """Return the controller only when *token* is current and unexpired."""
        current = self._unexpired_current()
        if current is None or not token or not secrets.compare_digest(current.token, token):
            raise ControlLeaseRequired()
        return current

    def heartbeat(self, token: str) -> ControlLease:
        """Extend the matching controller lease from the injected clock."""
        current = self.require(token)
        renewed = replace(current, expires_at=self._clock() + self._ttl)
        self._current = renewed
        return renewed

    def release(self, token: str) -> None:
        """Release only the matching unexpired controller lease."""
        self.require(token)
        self._current = None

    def _unexpired_current(self) -> ControlLease | None:
        current = self._current
        if current is not None and current.expires_at <= self._clock():
            self._current = None
            return None
        return current

"""Contract tests for the single-controller runtime lease."""

from datetime import datetime, timedelta, timezone

import pytest

from manyselves.application.control import (
    ControlLeaseHeld,
    ControlLeaseRequired,
    ControlLeaseService,
)


class MutableClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 7, 31, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, *, seconds: int) -> None:
        self.now += timedelta(seconds=seconds)


def test_second_client_cannot_take_an_unexpired_lease() -> None:
    clock = MutableClock()
    leases = ControlLeaseService(clock=clock, ttl=timedelta(seconds=30))
    current = leases.acquire(client_id="browser-1", actor_id="alice")

    with pytest.raises(ControlLeaseHeld) as raised:
        leases.acquire(client_id="browser-2", actor_id="bob")

    assert raised.value.controller_client_id == "browser-1"
    assert leases.require(current.token) == current


def test_same_client_reacquire_renews_without_creating_a_second_controller() -> None:
    clock = MutableClock()
    leases = ControlLeaseService(clock=clock, ttl=timedelta(seconds=30))
    original = leases.acquire(client_id="browser-1", actor_id="alice")
    clock.advance(seconds=10)

    renewed = leases.acquire(client_id="browser-1", actor_id="alice")

    assert renewed.token == original.token
    assert renewed.expires_at > original.expires_at
    assert leases.current_controller_client_id == "browser-1"


def test_heartbeat_extends_expiration() -> None:
    clock = MutableClock()
    leases = ControlLeaseService(clock=clock, ttl=timedelta(seconds=30))
    lease = leases.acquire(client_id="browser-1", actor_id="alice")
    clock.advance(seconds=20)

    renewed = leases.heartbeat(lease.token)

    assert renewed.expires_at == clock() + timedelta(seconds=30)
    clock.advance(seconds=20)
    assert leases.require(lease.token).client_id == "browser-1"


def test_release_clears_only_the_matching_controller() -> None:
    leases = ControlLeaseService(ttl=timedelta(seconds=30))
    lease = leases.acquire(client_id="browser-1", actor_id="alice")

    with pytest.raises(ControlLeaseRequired):
        leases.release("wrong-token")

    assert leases.current_controller_client_id == "browser-1"
    leases.release(lease.token)
    assert leases.current_controller_client_id is None
    with pytest.raises(ControlLeaseRequired):
        leases.require(lease.token)


def test_expired_lease_is_required_and_no_longer_inspected_as_controller() -> None:
    clock = MutableClock()
    leases = ControlLeaseService(clock=clock, ttl=timedelta(seconds=30))
    lease = leases.acquire(client_id="browser-1", actor_id="alice")
    clock.advance(seconds=31)

    assert leases.current_controller_client_id is None
    with pytest.raises(ControlLeaseRequired):
        leases.require(lease.token)

    replacement = leases.acquire(client_id="browser-2", actor_id="bob")
    assert replacement.client_id == "browser-2"


@pytest.mark.parametrize("token", ["", "unknown-token"])
def test_missing_or_unknown_token_requires_control(token: str) -> None:
    leases = ControlLeaseService(ttl=timedelta(seconds=30))
    leases.acquire(client_id="browser-1", actor_id="alice")

    with pytest.raises(ControlLeaseRequired):
        leases.require(token)


def test_lease_service_rejects_nonpositive_ttl() -> None:
    with pytest.raises(ValueError, match="positive"):
        ControlLeaseService(ttl=timedelta(0))

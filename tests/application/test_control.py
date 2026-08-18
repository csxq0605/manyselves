"""Contract tests for the single-controller runtime lease."""

from datetime import datetime, timedelta, timezone
from threading import Event, Lock, Thread

import pytest

from manyselves.application import control as control_module
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

    renewed = leases.acquire(
        client_id="browser-1",
        actor_id="alice",
        lease_token=original.token,
    )

    assert renewed.token == original.token
    assert renewed.expires_at > original.expires_at
    assert leases.current_controller_client_id == "browser-1"


@pytest.mark.parametrize("proof", [None, "wrong-token"])
def test_same_client_reacquire_requires_proof_of_the_current_token(
    proof: str | None,
) -> None:
    leases = ControlLeaseService(ttl=timedelta(seconds=30))
    original = leases.acquire(client_id="browser-1", actor_id="alice")

    with pytest.raises(ControlLeaseRequired):
        leases.acquire(
            client_id="browser-1",
            actor_id="alice",
            lease_token=proof,
        )

    assert leases.require(original.token) == original


def test_same_client_id_cannot_impersonate_a_different_actor() -> None:
    leases = ControlLeaseService(ttl=timedelta(seconds=30))
    original = leases.acquire(client_id="browser-1", actor_id="alice")

    with pytest.raises(ControlLeaseRequired):
        leases.acquire(
            client_id="browser-1",
            actor_id="mallory",
            lease_token=original.token,
        )

    assert leases.require(original.token).actor_id == "alice"


def test_different_client_remains_held_even_if_it_knows_the_token() -> None:
    leases = ControlLeaseService(ttl=timedelta(seconds=30))
    original = leases.acquire(client_id="browser-1", actor_id="alice")

    with pytest.raises(ControlLeaseHeld):
        leases.acquire(
            client_id="browser-2",
            actor_id="alice",
            lease_token=original.token,
        )


def test_control_lease_repr_never_discloses_token() -> None:
    lease = ControlLeaseService().acquire(client_id="browser-1", actor_id="alice")

    assert lease.token not in repr(lease)


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


def test_non_ascii_token_requires_control_instead_of_raising_type_error() -> None:
    leases = ControlLeaseService(ttl=timedelta(seconds=30))
    leases.acquire(client_id="browser-1", actor_id="alice")

    with pytest.raises(ControlLeaseRequired):
        leases.require("令牌-错误")


def test_non_ascii_reacquire_proof_requires_control() -> None:
    leases = ControlLeaseService(ttl=timedelta(seconds=30))
    original = leases.acquire(client_id="browser-1", actor_id="alice")

    with pytest.raises(ControlLeaseRequired):
        leases.acquire(
            client_id="browser-1",
            actor_id="alice",
            lease_token="令牌-错误",
        )

    assert leases.require(original.token) == original


def test_lease_service_rejects_nonpositive_ttl() -> None:
    with pytest.raises(ValueError, match="positive"):
        ControlLeaseService(ttl=timedelta(0))


def test_concurrent_initial_acquires_commit_exactly_one_controller(monkeypatch) -> None:
    first_in_token_generation = Event()
    allow_first_token = Event()
    second_started = Event()
    second_done = Event()
    call_count = 0
    call_count_lock = Lock()
    successes = []
    failures = []

    def controlled_token(_size: int) -> str:
        nonlocal call_count
        with call_count_lock:
            call_count += 1
            current_call = call_count
        if current_call == 1:
            first_in_token_generation.set()
            assert allow_first_token.wait(timeout=2)
        return f"token-{current_call}"

    monkeypatch.setattr(control_module.secrets, "token_urlsafe", controlled_token)
    leases = ControlLeaseService(ttl=timedelta(seconds=30))

    def acquire(client_id: str, *, started: Event | None = None, done: Event | None = None) -> None:
        if started is not None:
            started.set()
        try:
            successes.append(leases.acquire(client_id=client_id, actor_id=client_id))
        except Exception as exc:  # noqa: BLE001 - captured for deterministic thread assertion
            failures.append(exc)
        finally:
            if done is not None:
                done.set()

    first = Thread(target=acquire, args=("browser-1",), daemon=True)
    second = Thread(
        target=acquire,
        args=("browser-2",),
        kwargs={"started": second_started, "done": second_done},
        daemon=True,
    )
    first.start()
    second_was_started = False
    try:
        assert first_in_token_generation.wait(timeout=2)
        second.start()
        second_was_started = True
        assert second_started.wait(timeout=2)
        second_completed_before_first_commit = second_done.wait(timeout=0.2)
    finally:
        allow_first_token.set()
        first.join(timeout=2)
        if second_was_started:
            second.join(timeout=2)

    assert not first.is_alive()
    assert not second.is_alive()
    assert second_completed_before_first_commit is False
    assert len(successes) == 1
    assert len(failures) == 1
    assert isinstance(failures[0], ControlLeaseHeld)
    assert leases.current_controller_client_id == "browser-1"


def test_heartbeat_transaction_cannot_overwrite_released_replacement(monkeypatch) -> None:
    leases = ControlLeaseService(ttl=timedelta(seconds=30))
    original = leases.acquire(client_id="browser-1", actor_id="alice")
    first_in_validation = Event()
    allow_first_validation = Event()
    replacement_started = Event()
    replacement_done = Event()
    call_count = 0
    call_count_lock = Lock()
    original_compare = control_module._tokens_equal
    heartbeat_failures = []
    replacement_failures = []

    def controlled_compare(left: str, right: str) -> bool:
        nonlocal call_count
        with call_count_lock:
            call_count += 1
            current_call = call_count
        if current_call == 1:
            first_in_validation.set()
            assert allow_first_validation.wait(timeout=2)
        return original_compare(left, right)

    monkeypatch.setattr(control_module, "_tokens_equal", controlled_compare)

    def heartbeat() -> None:
        try:
            leases.heartbeat(original.token)
        except Exception as exc:  # noqa: BLE001 - captured for deterministic thread assertion
            heartbeat_failures.append(exc)

    def replace_controller() -> None:
        replacement_started.set()
        try:
            leases.release(original.token)
            leases.acquire(client_id="browser-2", actor_id="bob")
        except Exception as exc:  # noqa: BLE001 - captured for deterministic thread assertion
            replacement_failures.append(exc)
        finally:
            replacement_done.set()

    heartbeat_thread = Thread(target=heartbeat, daemon=True)
    replacement_thread = Thread(target=replace_controller, daemon=True)
    heartbeat_thread.start()
    replacement_was_started = False
    try:
        assert first_in_validation.wait(timeout=2)
        replacement_thread.start()
        replacement_was_started = True
        assert replacement_started.wait(timeout=2)
        replacement_completed_during_heartbeat = replacement_done.wait(timeout=0.2)
    finally:
        allow_first_validation.set()
        heartbeat_thread.join(timeout=2)
        if replacement_was_started:
            replacement_thread.join(timeout=2)

    assert not heartbeat_thread.is_alive()
    assert not replacement_thread.is_alive()
    assert replacement_completed_during_heartbeat is False
    assert heartbeat_failures == []
    assert replacement_failures == []
    assert leases.current_controller_client_id == "browser-2"

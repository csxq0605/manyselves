"""Concurrency contract for one-runtime, many-observer deployment."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from manyselves.application.control import ControlLeaseHeld, ControlLeaseService


def test_concurrent_clients_produce_exactly_one_runtime_controller() -> None:
    service = ControlLeaseService()

    def acquire(client_id: str) -> tuple[str, str]:
        try:
            lease = service.acquire(client_id=client_id, actor_id=client_id)
            return client_id, lease.token
        except ControlLeaseHeld:
            return client_id, ""

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(acquire, [f"client-{index}" for index in range(8)]))

    winners = [(client, token) for client, token in results if token]
    assert len(winners) == 1
    assert service.current_controller_client_id == winners[0][0]


def test_observer_can_take_control_only_after_lease_expiry() -> None:
    now = datetime(2026, 8, 3, tzinfo=timezone.utc)
    clock = [now]
    service = ControlLeaseService(clock=lambda: clock[0], ttl=timedelta(seconds=30))
    service.acquire(client_id="holder", actor_id="holder")

    with pytest.raises(ControlLeaseHeld):
        service.acquire(client_id="observer", actor_id="observer")

    clock[0] += timedelta(seconds=31)
    replacement = service.acquire(client_id="observer", actor_id="observer")
    assert replacement.client_id == "observer"

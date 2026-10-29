"""Maintenance and every Run start share one admission.

A scheduled automatic resume is a Run still in progress: maintenance must not
quiesce under it, and if it falls due during maintenance it waits.
"""

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from manyselves.application.control import ControlLeaseService
from manyselves.application.errors import MaintenanceQuiescedError, RuntimeBusyError
from manyselves.application.main_run_tools import ResumeRunTool
from manyselves.application.runtime_facade import RuntimeFacade
from manyselves.application.workflow_projection import WorkflowProjectionFacade
from manyselves.runtime.capability_binding import RuntimeBindingCatalog
from manyselves.runtime.run_failure import RunFailure


class _Binding:
    """A Run that failed with a temporary error; each resume calls the Provider."""

    capability_id = "neutral"

    def __init__(self, *, start_delay: float = 0.0) -> None:
        self.provider_calls = 0
        self.running = False
        self.start_delay = start_delay

    @property
    def active(self) -> bool:
        return self.running

    async def resume(self, command_id, run_id: str) -> dict[str, Any]:
        # Loading and compiling the Run takes a while before its task exists.
        await asyncio.sleep(self.start_delay)
        self.provider_calls += 1
        self.running = True
        return {"run_id": run_id, "task_id": None}

    def get_run(self, run_id: str) -> dict[str, Any]:
        return {
            "run": {
                "run_id": run_id,
                "capability_id": self.capability_id,
                "workflow_id": "root",
                "status": "running" if self.running else "failed",
            }
        }

    def cancel(self, run_id: str):
        return None

    async def close(self) -> None:
        return None


def _setup(tmp_path, *, delay: float = 0.05, start_delay: float = 0.0):
    leases = ControlLeaseService()
    facade = RuntimeFacade(
        SimpleNamespace(is_ready=True, workspace=tmp_path, loop_manager=None, backend=None),
        leases=leases,
    )
    lease = leases.acquire(client_id="backup", actor_id="admin").token
    binding = _Binding(start_delay=start_delay)
    bindings = RuntimeBindingCatalog()
    bindings.register(binding)
    projection = WorkflowProjectionFacade(
        tmp_path,
        runtime_services=None,
        runtime_bindings=bindings,
        admission=facade.run_admission,
    )
    projection.describe_failure = lambda run_id: RunFailure(  # type: ignore[method-assign]
        path=["turn"], action_kind="invoke_agent", error="HTTP 503", category="temporary",
        retryable=True,
    )
    assert projection.supervisor is not None
    projection.supervisor.resume_delay_seconds = delay
    return facade, lease, binding, projection


async def _quiesce(facade: RuntimeFacade, lease: str, projection) -> str:
    return await facade.quiesce(
        lease_token=lease, busy=lambda: projection.active, flush=lambda: None
    )


@pytest.mark.asyncio
async def test_a_scheduled_resume_keeps_maintenance_out(tmp_path) -> None:
    facade, lease, binding, projection = _setup(tmp_path)

    await projection.supervisor.on_settled("run-1")  # 503 -> resume scheduled
    assert not binding.active and projection.active

    with pytest.raises(RuntimeBusyError):
        await _quiesce(facade, lease, projection)
    await asyncio.sleep(0.15)

    assert binding.provider_calls == 1
    assert not facade.is_quiesced


@pytest.mark.asyncio
async def test_no_quiesce_slips_in_while_the_resume_starts(tmp_path) -> None:
    # The resume leaves the schedule before its Run task exists.
    facade, lease, binding, projection = _setup(tmp_path, delay=0.0, start_delay=0.1)

    await projection.supervisor.on_settled("run-1")
    await asyncio.sleep(0.02)  # the resume is inside binding.resume now
    assert not projection.supervisor.has_pending and not binding.active

    with pytest.raises(RuntimeBusyError):
        await _quiesce(facade, lease, projection)
    assert binding.provider_calls == 1 and not facade.is_quiesced


@pytest.mark.asyncio
async def test_a_resume_due_during_maintenance_waits_for_its_end(tmp_path) -> None:
    facade, lease, binding, projection = _setup(tmp_path)

    await projection.supervisor.on_settled("run-1")
    # Even if something entered maintenance anyway, the resume does not run in it.
    token = await facade.quiesce(lease_token=lease, busy=lambda: False, flush=lambda: None)
    await asyncio.sleep(0.15)
    assert binding.provider_calls == 0

    await facade.release_quiesce(lease_token=lease, maintenance_token=token)
    await asyncio.sleep(0.05)
    assert binding.provider_calls == 1


@pytest.mark.asyncio
async def test_main_cannot_start_a_run_during_maintenance(tmp_path) -> None:
    facade, lease, binding, projection = _setup(tmp_path)
    token = await _quiesce(facade, lease, projection)

    with pytest.raises(MaintenanceQuiescedError):
        await ResumeRunTool(projection_resolver=lambda: projection)(run_id="run-1")
    assert binding.provider_calls == 0
    await facade.release_quiesce(lease_token=lease, maintenance_token=token)


@pytest.mark.asyncio
async def test_a_notice_to_main_waits_for_maintenance_to_end(tmp_path) -> None:
    facade, lease, _binding, projection = _setup(tmp_path)
    published: list[str] = []

    class _Bus:
        async def publish(self, message) -> None:
            published.append(message.content)

    from manyselves.application.workflow_projection import _main_notifier

    notify = _main_notifier(SimpleNamespace(bus=_Bus()), projection.admission)
    token = await _quiesce(facade, lease, projection)
    sending = asyncio.create_task(notify("<run_notice/>"))
    await asyncio.sleep(0.05)
    assert published == []

    await facade.release_quiesce(lease_token=lease, maintenance_token=token)
    await asyncio.wait_for(sending, 1)
    assert published == ["<run_notice/>"]


@pytest.mark.asyncio
async def test_a_start_inside_a_mutation_transaction_is_not_admitted_twice(tmp_path) -> None:
    facade, lease, binding, projection = _setup(tmp_path)

    async with facade.mutation_transaction(lease):
        await asyncio.wait_for(projection.resume(None, "run-1"), 1)

    assert binding.provider_calls == 1

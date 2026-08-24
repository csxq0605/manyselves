"""Small lifecycle primitives for detached declarative Run execution."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID

from manyselves.kernel.workflow import WorkflowState

from .state_store import FileWorkflowStateStore

StartedCallback = Callable[[WorkflowState], None]


class DetachedRuntime(Protocol):
    """Explicit Runtime port for an application-owned detached start."""

    def run_id_for(self, command_id: UUID, workflow_id: str) -> str: ...

    async def start(
        self,
        command_id: UUID,
        workflow_id: str,
        values: Any,
    ) -> dict[str, Any]: ...

    async def resume(
        self,
        command_id: UUID,
        run_id: str,
    ) -> dict[str, Any]: ...


class StartAwareFileWorkflowStateStore(FileWorkflowStateStore):
    """File state store that reports the first persisted state for a Run.

    The callback is an in-process lifecycle signal only.  The persisted JSON
    remains the source of truth and all state transitions still belong to the
    generic Runtime Host.
    """

    def __init__(self, workspace: Path) -> None:
        super().__init__(workspace)
        self._started_callbacks: dict[str, StartedCallback] = {}

    def register_started(self, run_id: str, callback: StartedCallback) -> None:
        self._started_callbacks[run_id] = callback

    def unregister_started(self, run_id: str) -> None:
        self._started_callbacks.pop(run_id, None)

    def save(self, state: WorkflowState) -> None:
        super().save(state)
        callback = self._started_callbacks.pop(state.run_id, None)
        if callback is not None:
            callback(state)


class DetachedRunTaskOwner:
    """Own background execution tasks until the account runtime closes."""

    def __init__(self) -> None:
        self._tasks: set[asyncio.Task[Any]] = set()
        self._tasks_by_run: dict[str, asyncio.Task[Any]] = {}

    @property
    def active(self) -> bool:
        return any(not task.done() for task in self._tasks)

    def is_active(self, run_id: str) -> bool:
        """Return whether this process already owns execution for one Run."""

        task = self._tasks_by_run.get(run_id)
        return task is not None and not task.done()

    async def accept_after_persisted_state(
        self,
        *,
        run_id: str,
        state_store: StartAwareFileWorkflowStateStore,
        operation: Awaitable[dict[str, Any]],
    ) -> dict[str, Any]:
        """Return after the operation's next persisted state transition."""

        if self.is_active(run_id):
            return {"run_id": run_id, "task_id": None}

        started = asyncio.Event()
        state_store.register_started(run_id, lambda _state: started.set())
        task = asyncio.create_task(operation)
        self._tasks.add(task)
        self._tasks_by_run[run_id] = task
        task.add_done_callback(lambda completed: self._discard_task(run_id, completed))
        started_waiter = asyncio.create_task(started.wait())
        try:
            done, _pending = await asyncio.wait(
                {task, started_waiter},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if started_waiter in done:
                return {"run_id": run_id, "task_id": None}

            # Validation/compilation errors happen before the Host persists
            # its first state and remain synchronous API errors.  Once the
            # state signal was emitted, a later failure is represented by the
            # Host's persisted FAILED state and the accepted response stands.
            if not started.is_set():
                return task.result()
            try:
                return task.result()
            except BaseException:
                return {"run_id": run_id, "task_id": None}
        finally:
            started_waiter.cancel()
            if not started.is_set():
                state_store.unregister_started(run_id)

    def _discard_task(self, run_id: str, task: asyncio.Task[Any]) -> None:
        self._tasks.discard(task)
        if self._tasks_by_run.get(run_id) is task:
            self._tasks_by_run.pop(run_id, None)
        if task.cancelled():
            return
        # Retrieve the exception so a background failure is never reported as
        # an unobserved asyncio warning.  WorkflowRuntimeHost persists action
        # failures before propagating them.
        task.exception()

    async def close(self) -> None:
        """Stop account-owned detached operations during runtime shutdown."""

        tasks = tuple(self._tasks)
        if tasks:
            # Do not cancel after a state has been accepted: cancellation
            # would leave an authoritative Run in RUNNING.  The Host owns the
            # terminal transition, so account shutdown waits for it to settle.
            await asyncio.gather(*tasks, return_exceptions=True)


__all__ = [
    "DetachedRunTaskOwner",
    "DetachedRuntime",
    "StartAwareFileWorkflowStateStore",
]

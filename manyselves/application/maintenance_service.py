"""Application-wide quiesce coordination for maintenance windows."""

import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .conversation_service import ConversationService
from .python_run_service import PythonRunService
from .reporting_facade import ReportingFacade
from .runtime_facade import RuntimeFacade


class MaintenanceService:
    def __init__(
        self,
        facade: RuntimeFacade,
        conversations: ConversationService,
        reporting: ReportingFacade,
        python_runs: PythonRunService,
        *,
        config_manager: Any | None = None,
        extra_flush: Callable[[], None] | None = None,
    ) -> None:
        self.facade = facade
        self.conversations = conversations
        self.reporting = reporting
        self.python_runs = python_runs
        self.config_manager = config_manager
        self.extra_flush = extra_flush

    @property
    def quiesced(self) -> bool:
        return self.facade.is_quiesced

    @property
    def pending_work(self) -> bool:
        """Whether queued Agent or persistence work has not reached durability."""
        return self.conversations.pending_persistence or self._has_queued_agent_work()

    async def quiesce(self, lease_token: str) -> str:
        return await self.facade.quiesce(
            lease_token=lease_token,
            busy=self._busy,
            flush=self._flush,
        )

    async def release(self, lease_token: str, maintenance_token: str) -> None:
        await self.facade.release_quiesce(
            lease_token=lease_token, maintenance_token=maintenance_token
        )

    def _busy(self) -> bool:
        statuses = self.facade.snapshot().agent_statuses.values()
        return (
            any(status != "idle" for status in statuses)
            or self.pending_work
            or self.reporting.active
            or self.python_runs.active
        )

    def _has_queued_agent_work(self) -> bool:
        manager = getattr(self.facade._host, "loop_manager", None)  # noqa: SLF001
        loops = getattr(manager, "_loops", {})
        return any(
            not queue.empty()
            for loop in loops.values()
            if (queue := getattr(loop, "_message_queue", None)) is not None
        )

    def _flush(self) -> None:
        self.conversations.flush()
        self.reporting.flush()
        self._flush_config()
        if self.extra_flush is not None:
            self.extra_flush()

    def _flush_config(self) -> None:
        """Fsync the configured YAML without following a symlinked path."""
        settings = getattr(self.config_manager, "_settings", None)
        raw_path = getattr(settings, "config_path", None)
        if raw_path is None:
            return
        path = Path(raw_path)
        existing_components = [item for item in (path, *path.parents) if item.exists()]
        if any(item.is_symlink() for item in existing_components):
            raise ValueError("Maintenance config path must not contain a symlink")
        if not path.is_file():
            return
        with path.open("rb") as handle:
            os.fsync(handle.fileno())
        if os.name == "posix":
            descriptor = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)

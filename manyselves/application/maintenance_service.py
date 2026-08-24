"""Application-wide quiesce coordination for maintenance windows."""

import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .conversation_service import ConversationService
from .python_run_service import PythonRunService
from .runtime_facade import RuntimeFacade
from .workflow_projection import WorkflowProjectionFacade


class MaintenanceService:
    def __init__(
        self,
        facade: RuntimeFacade,
        conversations: ConversationService,
        workflow_runs: WorkflowProjectionFacade,
        python_runs: PythonRunService,
        *,
        config_manager: Any | None = None,
        extra_flush: Callable[[], None] | None = None,
    ) -> None:
        self.facade = facade
        self.conversations = conversations
        self.workflow_runs = workflow_runs
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
            or self.workflow_runs.active
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
        workspace = self.conversations.workspace.resolve()
        self._flush_workspace(workspace)
        self._flush_config()
        if self.extra_flush is not None:
            self.extra_flush()

    def _flush_workspace(self, workspace: Path) -> None:
        """Fsync all regular workspace files without traversing symbolic links."""
        if workspace.is_symlink() or not workspace.is_dir():
            raise ValueError("Maintenance workspace must be a real directory")

        def walk(directory: Path) -> None:
            with os.scandir(directory) as entries:
                children = list(entries)
            for entry in children:
                if entry.is_symlink():
                    continue
                candidate = Path(entry.path)
                if not candidate.absolute().is_relative_to(workspace):
                    raise ValueError("Maintenance path escapes the active workspace")
                if entry.is_dir(follow_symlinks=False):
                    walk(candidate)
                elif entry.is_file(follow_symlinks=False):
                    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                    descriptor = os.open(candidate, flags)
                    try:
                        os.fsync(descriptor)
                    finally:
                        os.close(descriptor)
            if os.name == "posix":
                flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(
                    os, "O_NOFOLLOW", 0
                )
                descriptor = os.open(directory, flags)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)

        walk(workspace)

    def _config_path(self) -> Path | None:
        settings = getattr(self.config_manager, "_settings", None)
        raw_path = getattr(settings, "config_path", None)
        return Path(raw_path) if raw_path is not None else None

    def _flush_config(self) -> None:
        """Fsync the configured YAML without following a symlinked path."""
        path = self._config_path()
        if path is None:
            return
        path = path.absolute()
        existing_components = [
            item
            for item in (path, *path.parents)
            if item.exists() or item.is_symlink()
        ]
        if any(item.is_symlink() for item in existing_components):
            raise ValueError("Maintenance config path must not contain a symlink")
        if not path.is_file():
            return
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        if os.name == "posix":
            descriptor = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)

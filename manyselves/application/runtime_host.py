"""Shared ownership boundary for the Manyselves runtime lifecycle."""

import asyncio
from collections.abc import Callable
from contextlib import suppress
from enum import Enum, auto
from pathlib import Path

from loguru import logger

from ..config import ConfigManager
from ..core.loops import LoopManager, MessageBus
from ..core.project_structure import ensure_project_structure
from ..utils import add_project_logging
from .backend_api import BackendAPIImpl
from .errors import RuntimeStartupError

LoopManagerFactory = Callable[[Path, ConfigManager, MessageBus], LoopManager]
WorkspaceInitializer = Callable[[Path], None]


class _LifecycleState(Enum):
    NEW = auto()
    STARTING = auto()
    READY = auto()
    STOPPING = auto()
    STOPPED = auto()
    FAILED = auto()


class RuntimeHost:
    """Create, start, and stop one shared Manyselves runtime."""

    def __init__(
        self,
        *,
        config_manager: ConfigManager,
        bus: MessageBus,
        backend: BackendAPIImpl,
        loop_manager_factory: LoopManagerFactory = LoopManager,
        project_logging_initializer: WorkspaceInitializer = add_project_logging,
        project_structure_initializer: WorkspaceInitializer = ensure_project_structure,
    ) -> None:
        """Initialize a host with production-relevant injectable dependencies."""
        self.config_manager = config_manager
        self.bus = bus
        self.backend = backend
        self._loop_manager_factory = loop_manager_factory
        self._project_logging_initializer = project_logging_initializer
        self._project_structure_initializer = project_structure_initializer

        self._loop_manager: LoopManager | None = None
        self._workspace: Path | None = None
        self._bus_task: asyncio.Task[None] | None = None
        self._bus_shutdown = False
        self._producers_stopped = False
        self._orderly_shutdown_draining = False
        self._state = _LifecycleState.NEW
        self._lifecycle_lock = asyncio.Lock()

    @classmethod
    def create(cls, config_manager: ConfigManager | None = None) -> "RuntimeHost":
        """Build a host with the normal production runtime dependencies."""
        if config_manager is None:
            config_manager = ConfigManager()
        bus = MessageBus()
        backend = BackendAPIImpl(config_manager=config_manager, bus=bus)
        return cls(config_manager=config_manager, bus=bus, backend=backend)

    @property
    def is_ready(self) -> bool:
        """Whether all runtime components completed startup."""
        return self._state is _LifecycleState.READY

    @property
    def workspace(self) -> Path | None:
        """Resolved workspace selected for this runtime."""
        return self._workspace

    @property
    def loop_manager(self) -> LoopManager | None:
        """Loop manager created for the selected workspace, if any."""
        return self._loop_manager

    @property
    def persistence_ready(self) -> bool:
        """Whether accepted bus events may still cross the durability boundary."""
        return self._state is _LifecycleState.READY or (
            self._state is _LifecycleState.STOPPING
            and self._orderly_shutdown_draining
        )

    async def start(self, workspace: Path) -> None:
        """Validate configuration and start runtime processing for a workspace."""
        async with self._lifecycle_lock:
            if self._state is _LifecycleState.READY:
                logger.warning("Runtime host already started")
                return
            if self._state in {
                _LifecycleState.STOPPING,
                _LifecycleState.STOPPED,
                _LifecycleState.FAILED,
            }:
                raise RuntimeStartupError(
                    "RUNTIME_STOPPED",
                    "Runtime host has already been stopped.",
                )

            is_valid, available = self.config_manager.validate_api_keys()
            if not is_valid:
                logger.warning("No API keys configured.")
                raise RuntimeStartupError("NO_PROVIDER_KEYS", "No API keys configured.")

            logger.info("Available providers: {}", available)
            self._state = _LifecycleState.STARTING

            try:
                resolved_workspace = Path(workspace).resolve()
                self._project_logging_initializer(resolved_workspace)
                self._project_structure_initializer(resolved_workspace)
                logger.debug("Ensured project structure in: {}", resolved_workspace)

                loop_manager = self._loop_manager_factory(
                    resolved_workspace,
                    self.config_manager,
                    self.bus,
                )
                self._bind_manager(loop_manager, resolved_workspace)
                self._bus_task = asyncio.create_task(
                    self.bus.process_queue(),
                    name="manyselves-message-bus",
                )
                await asyncio.sleep(0)
                await loop_manager.start()
            except BaseException:
                try:
                    await self._stop_locked()
                except BaseException:
                    logger.exception("Runtime cleanup failed after partial startup")
                raise

            self._state = _LifecycleState.READY
            logger.info(
                "Application started successfully with workspace: {}",
                resolved_workspace,
            )

    async def stop(self) -> None:
        """Stop runtime components and await only work owned by this host."""
        async with self._lifecycle_lock:
            await self._stop_locked()

    async def stop_producers(self) -> None:
        """Stop Agent producers while leaving the message bus available to drain."""
        async with self._lifecycle_lock:
            await self._stop_producers_locked()

    async def begin_orderly_shutdown(self) -> None:
        """Authorize accepted-event draining for the explicit service shutdown path."""
        async with self._lifecycle_lock:
            if self._state is _LifecycleState.READY:
                self._orderly_shutdown_draining = True

    async def stop_bus(self) -> None:
        """Stop and join the bus after application consumers have drained."""
        async with self._lifecycle_lock:
            await self._stop_bus_locked()

    async def switch_workspace(self, workspace: Path) -> None:
        """Replace workspace loops transactionally while retaining the single bus task."""
        async with self._lifecycle_lock:
            if self._state is not _LifecycleState.READY or self._loop_manager is None:
                raise RuntimeStartupError("RUNTIME_NOT_READY", "Runtime is not ready")

            resolved_workspace = Path(workspace).resolve()
            if resolved_workspace == self._workspace:
                return

            previous_workspace = self._workspace
            previous_manager = self._loop_manager
            self._state = _LifecycleState.STARTING
            try:
                await previous_manager.stop()
            except BaseException:
                self._orderly_shutdown_draining = False
                self._state = _LifecycleState.FAILED
                raise
            self._bind_manager(None, None)

            try:
                self._project_logging_initializer(resolved_workspace)
                self._project_structure_initializer(resolved_workspace)
                replacement = self._loop_manager_factory(
                    resolved_workspace,
                    self.config_manager,
                    self.bus,
                )
                self._bind_manager(replacement, resolved_workspace)
                await replacement.start()
            except BaseException as replacement_error:
                cleanup_error = await self._discard_bound_manager()
                if cleanup_error is not None:
                    self._orderly_shutdown_draining = False
                    self._state = _LifecycleState.FAILED
                    replacement_error.add_note(
                        f"Replacement cleanup did not finish: {cleanup_error!r}"
                    )
                    raise replacement_error from cleanup_error
                if previous_workspace is not None:
                    try:
                        restored = self._loop_manager_factory(
                            previous_workspace,
                            self.config_manager,
                            self.bus,
                        )
                        self._bind_manager(restored, previous_workspace)
                        await restored.start()
                    except BaseException as restore_error:
                        restore_cleanup_error = await self._discard_bound_manager()
                        self._orderly_shutdown_draining = False
                        self._state = _LifecycleState.FAILED
                        replacement_error.add_note(
                            f"Previous workspace restoration failed: {restore_error!r}"
                        )
                        if restore_cleanup_error is not None:
                            replacement_error.add_note(
                                "Restoration cleanup did not finish: "
                                f"{restore_cleanup_error!r}"
                            )
                            raise replacement_error from restore_cleanup_error
                        raise replacement_error from restore_error
                    self._state = _LifecycleState.READY
                raise replacement_error

            self._state = _LifecycleState.READY
            logger.info("Activated runtime workspace: {}", resolved_workspace)

    async def replace_loop_manager(self, *, recovery: bool = False) -> None:
        """Replace all provider-backed loops while retaining the shared message bus."""
        async with self._lifecycle_lock:
            workspace = self._workspace
            if workspace is None:
                raise RuntimeStartupError("RUNTIME_NOT_READY", "Runtime is not ready")
            if recovery:
                allowed = self._state is _LifecycleState.FAILED
            else:
                allowed = (
                    self._state is _LifecycleState.READY
                    and self._loop_manager is not None
                )
            if not allowed:
                raise RuntimeStartupError("RUNTIME_NOT_READY", "Runtime is not ready")

            self._orderly_shutdown_draining = False
            self._state = _LifecycleState.STARTING
            previous_manager = self._loop_manager
            if previous_manager is not None:
                try:
                    await previous_manager.stop()
                except BaseException:
                    self._state = _LifecycleState.FAILED
                    raise
            self._bind_manager(None, workspace)

            try:
                replacement = self._loop_manager_factory(
                    workspace,
                    self.config_manager,
                    self.bus,
                )
                self._bind_manager(replacement, workspace)
                await replacement.start()
            except BaseException as replacement_error:
                cleanup_error = await self._discard_provider_candidate(workspace)
                self._state = _LifecycleState.FAILED
                if cleanup_error is not None:
                    replacement_error.add_note(
                        "Provider runtime candidate cleanup did not finish."
                    )
                    raise replacement_error from None
                raise

            self._producers_stopped = False
            self._state = _LifecycleState.READY

    async def _discard_provider_candidate(
        self,
        workspace: Path,
    ) -> BaseException | None:
        """Stop a failed provider candidate and retain its workspace for recovery."""
        manager = self._loop_manager
        try:
            if manager is not None:
                await manager.stop()
        except BaseException as error:
            logger.error("Provider runtime candidate cleanup did not finish")
            return error
        self._bind_manager(None, workspace)
        return None

    async def mark_failed(self) -> None:
        """Make an owned runtime unavailable without discarding cleanup ownership."""
        async with self._lifecycle_lock:
            if self._state is not _LifecycleState.STOPPED:
                self._orderly_shutdown_draining = False
                self._state = _LifecycleState.FAILED

    async def fail_consistency(self) -> None:
        """Fail closed and immediately stop owned Agent producers, retaining cleanup."""
        async with self._lifecycle_lock:
            if self._state is _LifecycleState.STOPPED:
                return
            self._orderly_shutdown_draining = False
            self._state = _LifecycleState.FAILED
            manager = self._loop_manager
            if manager is not None and not self._producers_stopped:
                await manager.stop()
                self._producers_stopped = True

    def _bind_manager(self, manager: LoopManager | None, workspace: Path | None) -> None:
        """Change host/backend manager ownership together without an await boundary."""
        self._loop_manager = manager
        self._workspace = workspace
        self.backend.set_loop_manager(manager)

    async def _discard_bound_manager(self) -> BaseException | None:
        """Clear a candidate only after stop confirms that it no longer owns work."""
        manager = self._loop_manager
        try:
            if manager is not None:
                await manager.stop()
        except BaseException as error:
            logger.exception("Runtime loop candidate cleanup failed")
            return error
        self._bind_manager(None, None)
        return None

    async def _stop_locked(self) -> None:
        """Stop an active lifecycle while the lifecycle lock is held."""
        if self._state is _LifecycleState.STOPPED:
            return
        if self._state is _LifecycleState.NEW:
            self._state = _LifecycleState.STOPPED
            return

        producer_error: BaseException | None = None
        try:
            await self._stop_producers_locked()
        except BaseException as error:
            producer_error = error
        try:
            await self._stop_bus_locked()
        except BaseException as bus_error:
            if producer_error is None:
                raise
            producer_error.add_note(f"Runtime bus cleanup also failed: {bus_error!r}")
        if producer_error is not None:
            raise producer_error

    async def _stop_producers_locked(self) -> None:
        if self._state in {_LifecycleState.NEW, _LifecycleState.STOPPED}:
            return
        if self._state is not _LifecycleState.FAILED:
            self._state = _LifecycleState.STOPPING
        if self._loop_manager is not None and not self._producers_stopped:
            await self._loop_manager.stop()
            self._producers_stopped = True

    async def _stop_bus_locked(self) -> None:
        if self._state is _LifecycleState.STOPPED:
            return
        if self._state is _LifecycleState.NEW:
            self._orderly_shutdown_draining = False
            self._state = _LifecycleState.STOPPED
            return
        self._orderly_shutdown_draining = False
        if self._state is not _LifecycleState.FAILED:
            self._state = _LifecycleState.STOPPING
        if not self._bus_shutdown:
            self.bus.shutdown()
            self._bus_shutdown = True
        bus_task = self._bus_task
        self._bus_task = None
        if bus_task is not None:
            if not bus_task.done():
                bus_task.cancel()
            with suppress(asyncio.CancelledError):
                await bus_task
        self._state = (
            _LifecycleState.STOPPED
            if self._producers_stopped
            else _LifecycleState.FAILED
        )

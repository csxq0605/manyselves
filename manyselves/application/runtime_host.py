"""Shared ownership boundary for the Manyselves runtime lifecycle."""

import asyncio
from collections.abc import Callable
from contextlib import suppress
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
        self._is_ready = False
        self._lifecycle_active = False
        self._stop_lock = asyncio.Lock()

    @classmethod
    def create(cls) -> "RuntimeHost":
        """Build a host with the normal production runtime dependencies."""
        config_manager = ConfigManager()
        bus = MessageBus()
        backend = BackendAPIImpl(config_manager=config_manager, bus=bus)
        return cls(config_manager=config_manager, bus=bus, backend=backend)

    @property
    def is_ready(self) -> bool:
        """Whether all runtime components completed startup."""
        return self._is_ready

    @property
    def workspace(self) -> Path | None:
        """Resolved workspace selected for this runtime."""
        return self._workspace

    @property
    def loop_manager(self) -> LoopManager | None:
        """Loop manager created for the selected workspace, if any."""
        return self._loop_manager

    async def start(self, workspace: Path) -> None:
        """Validate configuration and start runtime processing for a workspace."""
        if self._is_ready:
            logger.warning("Runtime host already started")
            return

        is_valid, available = self.config_manager.validate_api_keys()
        if not is_valid:
            logger.warning("No API keys configured.")
            raise RuntimeStartupError("NO_PROVIDER_KEYS", "No API keys configured.")

        logger.info("Available providers: {}", available)

        resolved_workspace = Path(workspace).resolve()
        self._workspace = resolved_workspace
        self._project_logging_initializer(resolved_workspace)
        self._project_structure_initializer(resolved_workspace)
        logger.debug("Ensured project structure in: {}", resolved_workspace)

        self._loop_manager = self._loop_manager_factory(
            resolved_workspace,
            self.config_manager,
            self.bus,
        )
        self._lifecycle_active = True

        try:
            self.backend.set_loop_manager(self._loop_manager)
            self._bus_task = asyncio.create_task(
                self.bus.process_queue(),
                name="manyselves-message-bus",
            )
            await asyncio.sleep(0)
            await self._loop_manager.start()
        except BaseException:
            try:
                await self.stop()
            except BaseException:
                logger.exception("Runtime cleanup failed after partial startup")
            raise

        self._is_ready = True
        logger.info("Application started successfully with workspace: {}", resolved_workspace)

    async def stop(self) -> None:
        """Stop runtime components and await only work owned by this host."""
        async with self._stop_lock:
            self._is_ready = False
            if not self._lifecycle_active and self._bus_task is None:
                return

            self._lifecycle_active = False
            self.bus.shutdown()

            try:
                if self._loop_manager is not None:
                    await self._loop_manager.stop()
            finally:
                bus_task = self._bus_task
                self._bus_task = None
                if bus_task is not None:
                    if not bus_task.done():
                        bus_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await bus_task

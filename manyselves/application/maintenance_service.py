"""Application-wide quiesce coordination for maintenance windows."""

from collections.abc import Callable

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
        extra_flush: Callable[[], None] | None = None,
    ) -> None:
        self.facade = facade
        self.conversations = conversations
        self.reporting = reporting
        self.python_runs = python_runs
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
        if self.extra_flush is not None:
            self.extra_flush()

"""Read-only adapter over the current in-process runtime implementation."""

from .models import RuntimeSnapshot
from .runtime_host import RuntimeHost
from .runtime_state import RuntimeStateProjection


class LegacyRuntimeAdapter:
    """Build client snapshots without leaking runtime implementation details."""

    def __init__(
        self, host: RuntimeHost, *, state: RuntimeStateProjection | None = None
    ) -> None:
        self._host = host
        self.state = state or RuntimeStateProjection()

    def snapshot(self, *, controller_client_id: str | None) -> RuntimeSnapshot:
        """Read the current runtime through public host/manager queries."""
        if not self._host.is_ready:
            return RuntimeSnapshot(
                ready=False,
                workspace=None,
                agent_statuses={},
                active_session_id=None,
                controller_client_id=controller_client_id,
            )

        manager = self._host.loop_manager
        workspace = self._host.workspace
        statuses = manager.get_all_agent_statuses() if manager is not None else {}
        session_id = manager.get_agent_session_id("main") if manager is not None else None
        recovery = self.state.build(manager, statuses)
        return RuntimeSnapshot(
            ready=True,
            workspace=str(workspace) if workspace is not None else None,
            agent_statuses=statuses,
            active_session_id=session_id,
            controller_client_id=controller_client_id,
            **recovery,
        )

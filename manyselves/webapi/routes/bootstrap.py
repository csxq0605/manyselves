"""Read-only health and initial-state routes."""

from fastapi import APIRouter, Request

from ..schemas.bootstrap import BootstrapSettings, BootstrapSnapshot, ProjectSnapshot
from ..schemas.runtime import RuntimeSnapshotResponse
from ..tenant_runtime import request_runtime_state

router = APIRouter()

@router.get("/bootstrap", response_model=BootstrapSnapshot)
async def bootstrap(request: Request) -> BootstrapSnapshot:
    """Return the first client snapshot from the active runtime facade."""
    state = request_runtime_state(request)
    facade = state.runtime_facade
    settings = state.web_settings if hasattr(state, "web_settings") else request.app.state.web_settings
    async with facade.read_transaction():
        runtime = facade.snapshot()
        registry = state.project_registry
        runtime = runtime.model_copy(update={"workspace": registry.active_project_id})
        public_runtime = RuntimeSnapshotResponse.from_runtime(runtime)
        return BootstrapSnapshot(
            streamId=state.event_broker.stream_id,
            runtime=public_runtime,
            project=ProjectSnapshot(id=registry.active_project_id),
            conversations=state.conversation_service.list("main")[0],
            agents=runtime.agent_statuses,
            settings=BootstrapSettings(
                sse_replay_capacity=settings.sse_replay_capacity,
                sse_client_queue_capacity=settings.sse_client_queue_capacity,
                control_lease_seconds=settings.control_lease_seconds,
            ),
            maintenance={"quiesced": facade.is_quiesced},
        )

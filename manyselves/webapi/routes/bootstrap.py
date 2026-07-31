"""Read-only health and initial-state routes."""

from fastapi import APIRouter, Request

from ..schemas.bootstrap import BootstrapSettings, BootstrapSnapshot, ProjectSnapshot

router = APIRouter()

@router.get("/bootstrap", response_model=BootstrapSnapshot)
async def bootstrap(request: Request) -> BootstrapSnapshot:
    """Return the first client snapshot from the active runtime facade."""
    facade = request.app.state.runtime_facade
    settings = request.app.state.web_settings
    async with facade.read_transaction():
        runtime = facade.snapshot()
        registry = request.app.state.project_registry
        runtime = runtime.model_copy(update={"workspace": registry.active_project_id})
        return BootstrapSnapshot(
            runtime=runtime,
            project=ProjectSnapshot(id=registry.active_project_id),
            conversations=[],
            agents=runtime.agent_statuses,
            settings=BootstrapSettings(
                sse_replay_capacity=settings.sse_replay_capacity,
                sse_client_queue_capacity=settings.sse_client_queue_capacity,
                control_lease_seconds=settings.control_lease_seconds,
            ),
        )

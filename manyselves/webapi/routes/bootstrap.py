"""Read-only health and initial-state routes."""

from fastapi import APIRouter, Request, Response, status

from ..schemas.bootstrap import BootstrapSettings, BootstrapSnapshot, ProjectSnapshot

router = APIRouter()


@router.get("/health/ready", status_code=status.HTTP_200_OK)
async def readiness(request: Request, response: Response) -> dict[str, str]:
    """Report whether the lifespan-owned runtime can accept work."""
    host = getattr(request.app.state, "runtime_host", None)
    if host is None or not host.is_ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "not_ready"}
    return {"status": "ready"}


@router.get("/bootstrap", response_model=BootstrapSnapshot)
async def bootstrap(request: Request) -> BootstrapSnapshot:
    """Return the first client snapshot from the active runtime facade."""
    facade = request.app.state.runtime_facade
    settings = request.app.state.web_settings
    async with facade.read_transaction():
        runtime = facade.snapshot()
        project_path = settings.data_root / settings.initial_project_id
        return BootstrapSnapshot(
            runtime=runtime,
            project=ProjectSnapshot(id=settings.initial_project_id, path=str(project_path)),
            conversations=[],
            agents=runtime.agent_statuses,
            settings=BootstrapSettings(
                sse_replay_capacity=settings.sse_replay_capacity,
                sse_client_queue_capacity=settings.sse_client_queue_capacity,
                control_lease_seconds=settings.control_lease_seconds,
            ),
        )

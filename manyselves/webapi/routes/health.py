"""Read-only health routes."""

from fastapi import APIRouter, Request, Response, status

router = APIRouter()


@router.get("/health/ready", status_code=status.HTTP_200_OK)
async def readiness(request: Request, response: Response) -> dict[str, str]:
    """Report whether the lifespan-owned runtime can accept work."""
    host = getattr(request.app.state, "runtime_host", None)
    if host is None or not host.is_ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "not_ready"}
    return {"status": "ready"}

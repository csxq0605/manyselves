"""Read-only health routes."""

from fastapi import APIRouter, Request, status

from ..errors import ApiError
from ..schemas.common import ErrorEnvelope

router = APIRouter()


@router.get(
    "/health/ready",
    status_code=status.HTTP_200_OK,
    responses={503: {"model": ErrorEnvelope}},
)
async def readiness(request: Request) -> dict[str, str]:
    """Report whether the lifespan-owned runtime can accept work."""
    host = getattr(request.app.state, "runtime_host", None)
    if host is None or not host.is_ready:
        raise ApiError(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            code="RUNTIME_NOT_READY",
            message="Runtime is not ready",
            retryable=True,
        )
    maintenance = getattr(request.app.state, "maintenance_service", None)
    if maintenance is not None and maintenance.quiesced:
        raise ApiError(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            code="MAINTENANCE_QUIESCED",
            message="Runtime mutations are disabled during maintenance",
            retryable=True,
        )
    return {"status": "ready"}

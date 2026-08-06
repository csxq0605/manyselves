"""Read-only health routes."""

from fastapi import APIRouter, Request, status

from ..errors import ApiError
from ..schemas.common import ErrorEnvelope

router = APIRouter()


@router.get("/health/live", status_code=status.HTTP_200_OK)
async def liveness() -> dict[str, str]:
    """Report that the API process is alive without claiming runtime readiness."""
    return {"status": "live"}


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


@router.get("/health/providers", status_code=status.HTTP_200_OK)
async def provider_status(request: Request) -> dict[str, str | bool]:
    """Check if LLM providers are configured.

    Returns:
        - configured: True if at least one provider is available
        - message: Human-readable status message
    """
    host = getattr(request.app.state, "runtime_host", None)
    if host is None or host.loop_manager is None:
        return {
            "configured": False,
            "message": "Runtime not initialized",
        }

    available = host.loop_manager._provider_manager.get_available_providers()
    if not available:
        return {
            "configured": False,
            "message": "No LLM providers configured. Configure in UI settings.",
        }

    return {
        "configured": True,
        "message": f"{len(available)} provider(s) available",
    }

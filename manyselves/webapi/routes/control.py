"""Authenticated endpoints for the process-local runtime controller lease."""

from fastapi import APIRouter, Depends, Response, status

from ...application.control import ControlLeaseHeld, ControlLeaseRequired
from ..dependencies import get_runtime_facade
from ..errors import ApiError
from ..schemas.control import LeaseAcquireRequest, LeaseResponse, LeaseTokenRequest
from ..security import require_deployment_access

router = APIRouter(dependencies=[Depends(require_deployment_access)])


def _lease_error(error: ControlLeaseHeld | ControlLeaseRequired) -> ApiError:
    """Map application lease failures to their public HTTP contract."""
    if isinstance(error, ControlLeaseHeld):
        return ApiError(
            status_code=status.HTTP_409_CONFLICT,
            code=error.code,
            message=str(error),
            retryable=True,
            details={"controllerClientId": error.controller_client_id},
        )
    return ApiError(
        status_code=status.HTTP_423_LOCKED,
        code=error.code,
        message=str(error),
        retryable=False,
    )


@router.post("/control/lease", response_model=LeaseResponse, status_code=status.HTTP_201_CREATED)
async def acquire_lease(request: LeaseAcquireRequest, facade=Depends(get_runtime_facade)) -> LeaseResponse:
    """Acquire a controller lease, or renew the same client's matching lease."""
    if facade is None:
        raise RuntimeError("Runtime facade is unavailable before application startup")
    try:
        lease = facade.leases.acquire(
            client_id=request.client_id,
            actor_id=request.actor_id or request.client_id,
            lease_token=request.lease_token,
        )
    except (ControlLeaseHeld, ControlLeaseRequired) as error:
        raise _lease_error(error) from error
    return LeaseResponse.from_lease(lease)


@router.post("/control/lease/heartbeat", response_model=LeaseResponse)
async def heartbeat_lease(request: LeaseTokenRequest, facade=Depends(get_runtime_facade)) -> LeaseResponse:
    """Extend the current controller lease when its token matches."""
    if facade is None:
        raise RuntimeError("Runtime facade is unavailable before application startup")
    try:
        lease = facade.leases.heartbeat(request.lease_token)
    except ControlLeaseRequired as error:
        raise _lease_error(error) from error
    return LeaseResponse.from_lease(lease)


@router.delete("/control/lease", status_code=status.HTTP_204_NO_CONTENT)
async def release_lease(request: LeaseTokenRequest, facade=Depends(get_runtime_facade)) -> Response:
    """Release the current controller lease when its token matches."""
    if facade is None:
        raise RuntimeError("Runtime facade is unavailable before application startup")
    try:
        facade.leases.release(request.lease_token)
    except ControlLeaseRequired as error:
        raise _lease_error(error) from error
    return Response(status_code=status.HTTP_204_NO_CONTENT)

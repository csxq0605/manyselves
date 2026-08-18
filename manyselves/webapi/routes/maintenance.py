"""Maintenance quiesce routes."""

from fastapi import APIRouter, Depends, Request

from ...application.control import ControlLeaseRequired
from ...application.errors import (
    MaintenanceQuiescedError,
    MaintenanceTokenMismatchError,
    RuntimeBusyError,
    RuntimeNotReadyError,
)
from ..errors import ApiError
from ..schemas.maintenance import MaintenanceReleaseRequest, MaintenanceResponse
from ..security import require_authenticated_session, require_control_lease_header
from ..tenant_runtime import request_runtime_state

router = APIRouter(prefix="/maintenance", dependencies=[Depends(require_authenticated_session)])


def _error(error: Exception) -> ApiError:
    if isinstance(error, RuntimeBusyError):
        return ApiError(status_code=409, code=error.code, message="Runtime has active work", retryable=True)
    if isinstance(error, MaintenanceQuiescedError):
        return ApiError(status_code=409, code=error.code, message=str(error), retryable=False)
    if isinstance(error, MaintenanceTokenMismatchError):
        return ApiError(status_code=409, code=error.code, message=str(error), retryable=False)
    if isinstance(error, ControlLeaseRequired):
        return ApiError(status_code=423, code=error.code, message=str(error), retryable=False)
    if isinstance(error, RuntimeNotReadyError):
        return ApiError(status_code=503, code=error.code, message=str(error), retryable=True)
    raise error


@router.post("/quiesce", response_model=MaintenanceResponse)
async def quiesce(
    request: Request,
    lease_token: str = Depends(require_control_lease_header),
):
    try:
        token = await request_runtime_state(request).maintenance_service.quiesce(lease_token)
        return MaintenanceResponse(quiesced=True, maintenanceToken=token)
    except Exception as error:
        raise _error(error) from error


@router.post("/release", response_model=MaintenanceResponse)
async def release(
    body: MaintenanceReleaseRequest,
    request: Request,
    lease_token: str = Depends(require_control_lease_header),
):
    try:
        await request_runtime_state(request).maintenance_service.release(
            lease_token, body.maintenance_token
        )
        return MaintenanceResponse(quiesced=False)
    except Exception as error:
        raise _error(error) from error

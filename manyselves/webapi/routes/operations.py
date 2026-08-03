"""Trusted server Python operation routes."""

from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, Request

from ...application.control import ControlLeaseRequired
from ...application.errors import (
    CommandIdConflictError,
    MaintenanceQuiescedError,
    RuntimeNotReadyError,
)
from ...application.python_run_service import (
    InvalidPythonPathError,
    PythonOperationNotFoundError,
    PythonRunUnsupportedError,
)
from ..errors import ApiError
from ..schemas.operations import (
    OperationAcceptedResponse,
    PythonOperationResponse,
    PythonRunRequest,
)
from ..security import require_authenticated_session, require_control_lease_header

router = APIRouter(prefix="/operations", dependencies=[Depends(require_authenticated_session)])


def _response(operation) -> PythonOperationResponse:
    return PythonOperationResponse(
        operationId=operation.operation_id,
        path=operation.path,
        arguments=operation.arguments,
        status=operation.status,
        stdout=operation.stdout.decode("utf-8", errors="replace"),
        stderr=operation.stderr.decode("utf-8", errors="replace"),
        stdoutTruncated=operation.stdout_truncated,
        stderrTruncated=operation.stderr_truncated,
        returnCode=operation.return_code,
        startedAt=operation.started_at,
        completedAt=operation.completed_at,
    )


def _error(error: Exception) -> ApiError:
    if isinstance(error, PythonRunUnsupportedError):
        return ApiError(
            status_code=501,
            code=error.code,
            message=str(error),
            retryable=False,
            details={
                "supportedPlatform": "posix",
                "deploymentTarget": "linux-compose",
            },
        )
    if isinstance(error, InvalidPythonPathError):
        return ApiError(status_code=422, code=error.code, message=str(error), retryable=False)
    if isinstance(error, PythonOperationNotFoundError):
        return ApiError(status_code=404, code=error.code, message="Python operation was not found", retryable=False)
    if isinstance(error, ValueError):
        return ApiError(status_code=422, code="INVALID_OPERATION_STATE", message=str(error), retryable=False)
    if isinstance(error, CommandIdConflictError):
        return ApiError(status_code=409, code=error.code, message=str(error), retryable=False)
    if isinstance(error, ControlLeaseRequired):
        return ApiError(status_code=423, code=error.code, message=str(error), retryable=False)
    if isinstance(error, RuntimeNotReadyError):
        return ApiError(status_code=503, code=error.code, message=str(error), retryable=True)
    if isinstance(error, MaintenanceQuiescedError):
        return ApiError(status_code=409, code=error.code, message=str(error), retryable=True)
    raise error


@router.post("/python", response_model=OperationAcceptedResponse, status_code=202)
async def run_python(
    body: PythonRunRequest,
    request: Request,
    command_id: UUID | None = Header(default=None, alias="Idempotency-Key"),
    lease_token: str = Depends(require_control_lease_header),
):
    try:
        command_id = command_id or uuid4()
        async with request.app.state.runtime_facade.mutation_transaction(lease_token):
            operation = request.app.state.python_run_service.start(
                command_id, body.path, body.arguments
            )
            return OperationAcceptedResponse(
                commandId=command_id, operationId=operation.operation_id
            )
    except Exception as error:
        raise _error(error) from error


@router.get("/{operation_id}", response_model=PythonOperationResponse)
async def get_operation(operation_id: str, request: Request):
    try:
        return _response(request.app.state.python_run_service.get(operation_id))
    except PythonOperationNotFoundError as error:
        raise _error(error) from error


@router.post("/{operation_id}/interrupt", response_model=PythonOperationResponse, status_code=202)
async def interrupt_operation(
    operation_id: str,
    request: Request,
    lease_token: str = Depends(require_control_lease_header),
):
    try:
        async with request.app.state.runtime_facade.mutation_transaction(lease_token):
            return _response(await request.app.state.python_run_service.interrupt(operation_id))
    except Exception as error:
        raise _error(error) from error

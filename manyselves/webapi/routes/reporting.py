"""Named reporting lifecycle commands and durable run snapshots."""

from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request

from ...application.control import ControlLeaseRequired
from ...application.errors import (
    CommandIdConflictError,
    MaintenanceQuiescedError,
    RuntimeNotReadyError,
)
from ...application.reporting_facade import (
    ReportingInvalidTransitionError,
    ReportingNotFoundError,
)
from ..errors import ApiError
from ..schemas.reporting import (
    ReportingAcceptedResponse,
    ReportingDecisionRequest,
    ReportingListResponse,
    ReportingResumeRequest,
    ReportingRevisionRequest,
    ReportingSnapshotResponse,
    ReportingStartRequest,
)
from ..security import require_control_lease_header, require_deployment_access

router = APIRouter(prefix="/reporting")


def _error(error: Exception) -> ApiError:
    if isinstance(error, ReportingNotFoundError):
        return ApiError(status_code=404, code=error.code, message="Reporting run was not found", retryable=False)
    if isinstance(error, ReportingInvalidTransitionError):
        return ApiError(status_code=422, code=error.code, message=str(error), retryable=False)
    if isinstance(error, FileNotFoundError):
        return ApiError(status_code=404, code="REPORT_RUN_NOT_FOUND", message=str(error), retryable=False)
    if isinstance(error, ValueError):
        return ApiError(status_code=422, code="REPORT_INVALID_TRANSITION", message=str(error), retryable=False)
    if isinstance(error, CommandIdConflictError):
        return ApiError(status_code=409, code=error.code, message=str(error), retryable=False)
    if isinstance(error, ControlLeaseRequired):
        return ApiError(status_code=423, code=error.code, message=str(error), retryable=False)
    if isinstance(error, RuntimeNotReadyError):
        return ApiError(status_code=503, code=error.code, message=str(error), retryable=True)
    if isinstance(error, MaintenanceQuiescedError):
        return ApiError(status_code=409, code=error.code, message=str(error), retryable=True)
    raise error


def _accepted(command_id: UUID, payload: dict) -> ReportingAcceptedResponse:
    return ReportingAcceptedResponse(
        commandId=command_id,
        runId=payload["run_id"],
        taskId=payload.get("task_id"),
    )


@router.get("/runs", response_model=ReportingListResponse)
async def list_runs(request: Request):
    facade = request.app.state.runtime_facade
    async with facade.read_transaction():
        return ReportingListResponse(runs=request.app.state.reporting_facade.list_runs())


@router.get("/runs/{run_id}", response_model=ReportingSnapshotResponse)
async def get_run(run_id: str, request: Request):
    facade = request.app.state.runtime_facade
    try:
        async with facade.read_transaction():
            return ReportingSnapshotResponse.model_validate(
                request.app.state.reporting_facade.snapshot(run_id)
            )
    except ReportingNotFoundError as error:
        raise _error(error) from error


@router.post("/runs", response_model=ReportingAcceptedResponse, status_code=202)
async def start_run(
    body: ReportingStartRequest,
    request: Request,
    command_id: UUID = Header(alias="Idempotency-Key"),
    _access: None = Depends(require_deployment_access),
    lease_token: str = Depends(require_control_lease_header),
):
    try:
        async with request.app.state.runtime_facade.mutation_transaction(lease_token):
            return _accepted(command_id, request.app.state.reporting_facade.start(command_id, body.to_core()))
    except Exception as error:
        raise _error(error) from error


@router.post("/runs/{run_id}/resume", response_model=ReportingAcceptedResponse, status_code=202)
async def resume_run(
    run_id: str,
    body: ReportingResumeRequest,
    request: Request,
    command_id: UUID = Header(alias="Idempotency-Key"),
    _access: None = Depends(require_deployment_access),
    lease_token: str = Depends(require_control_lease_header),
):
    try:
        async with request.app.state.runtime_facade.mutation_transaction(lease_token):
            payload = request.app.state.reporting_facade.resume_run(
                command_id,
                run_id,
                max_provider_attempts=body.max_provider_attempts,
                max_total_tokens=body.max_total_tokens,
                supplements=body.supplements,
            )
            return _accepted(command_id, payload)
    except Exception as error:
        raise _error(error) from error


@router.post("/decisions/{decision_id}/resume", response_model=ReportingAcceptedResponse, status_code=202)
async def resume_decision(
    decision_id: str,
    body: ReportingDecisionRequest,
    request: Request,
    command_id: UUID = Header(alias="Idempotency-Key"),
    _access: None = Depends(require_deployment_access),
    lease_token: str = Depends(require_control_lease_header),
):
    try:
        async with request.app.state.runtime_facade.mutation_transaction(lease_token):
            payload = request.app.state.reporting_facade.resume_decision(
                command_id, decision_id, body.action, body.supplements
            )
            return _accepted(command_id, payload)
    except Exception as error:
        raise _error(error) from error


@router.post("/revisions", response_model=ReportingAcceptedResponse, status_code=202)
async def revise(
    body: ReportingRevisionRequest,
    request: Request,
    command_id: UUID = Header(alias="Idempotency-Key"),
    _access: None = Depends(require_deployment_access),
    lease_token: str = Depends(require_control_lease_header),
):
    try:
        async with request.app.state.runtime_facade.mutation_transaction(lease_token):
            return _accepted(
                command_id,
                request.app.state.reporting_facade.revise(command_id, body.to_core()),
            )
    except Exception as error:
        raise _error(error) from error


@router.post("/runs/{run_id}/cancel", response_model=ReportingAcceptedResponse, status_code=202)
async def cancel_run(
    run_id: str,
    request: Request,
    command_id: UUID = Header(alias="Idempotency-Key"),
    _access: None = Depends(require_deployment_access),
    lease_token: str = Depends(require_control_lease_header),
):
    try:
        async with request.app.state.runtime_facade.mutation_transaction(lease_token):
            return _accepted(
                command_id,
                request.app.state.reporting_facade.cancel(command_id, run_id),
            )
    except Exception as error:
        raise _error(error) from error

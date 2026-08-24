"""Generic declarative Capability, Workflow, and Run endpoints."""

from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request
from pydantic import ValidationError

from ...application.control import ControlLeaseRequired
from ...application.errors import (
    CommandIdConflictError,
    MaintenanceQuiescedError,
    RuntimeNotReadyError,
)
from ...application.workflow_projection import (
    WorkflowInputError,
    WorkflowNotRunnableError,
    WorkflowProjectionFacade,
    WorkflowProjectionNotFoundError,
)
from ...runtime.capability_binding import (
    CapabilityRunInputError,
    CapabilityRunNotFoundError,
    CapabilityRunStateError,
)
from ..errors import ApiError
from ..schemas.workflows import (
    CapabilityListResponse,
    WorkflowCostResponse,
    WorkflowEventListResponse,
    WorkflowInputSchemaResponse,
    WorkflowListResponse,
    WorkflowOutputListResponse,
    WorkflowRunAcceptedResponse,
    WorkflowRunInputRequest,
    WorkflowRunListResponse,
    WorkflowRunResponse,
    WorkflowRunStartRequest,
)
from ..security import require_authenticated_session, require_control_lease_header
from ..tenant_runtime import request_runtime_state

router = APIRouter(dependencies=[Depends(require_authenticated_session)])


def _projection(request: Request) -> WorkflowProjectionFacade:
    return request_runtime_state(request).workflow_projection


def _error(error: Exception) -> ApiError:
    if isinstance(error, (WorkflowProjectionNotFoundError, CapabilityRunNotFoundError)):
        return ApiError(
            status_code=404,
            code="WORKFLOW_RESOURCE_NOT_FOUND",
            message=str(error),
            retryable=False,
        )
    if isinstance(
        error,
        (
            WorkflowNotRunnableError,
            WorkflowInputError,
            CapabilityRunInputError,
            ValidationError,
            ValueError,
        ),
    ):
        return ApiError(
            status_code=422,
            code="WORKFLOW_INPUT_INVALID",
            message=str(error),
            retryable=False,
        )
    if isinstance(error, CapabilityRunStateError):
        return ApiError(
            status_code=500,
            code="WORKFLOW_STATE_INVALID",
            message=str(error),
            retryable=False,
        )
    if isinstance(error, CommandIdConflictError):
        return ApiError(
            status_code=409,
            code=error.code,
            message=str(error),
            retryable=False,
        )
    if isinstance(error, ControlLeaseRequired):
        return ApiError(
            status_code=423,
            code=error.code,
            message=str(error),
            retryable=False,
        )
    if isinstance(error, RuntimeNotReadyError):
        return ApiError(
            status_code=503,
            code=error.code,
            message=str(error),
            retryable=True,
        )
    if isinstance(error, MaintenanceQuiescedError):
        return ApiError(
            status_code=409,
            code=error.code,
            message=str(error),
            retryable=True,
        )
    raise error


@router.get("/capabilities", response_model=CapabilityListResponse)
async def list_capabilities(request: Request):
    state = request_runtime_state(request)
    try:
        async with state.runtime_facade.read_transaction():
            return CapabilityListResponse(
                capabilities=_projection(request).list_capabilities()
            )
    except Exception as error:
        raise _error(error) from error


@router.get("/workflows", response_model=WorkflowListResponse)
async def list_workflows(request: Request):
    state = request_runtime_state(request)
    try:
        async with state.runtime_facade.read_transaction():
            return WorkflowListResponse(workflows=_projection(request).list_workflows())
    except Exception as error:
        raise _error(error) from error


@router.get(
    "/workflows/{workflow_id}/input-schema",
    response_model=WorkflowInputSchemaResponse,
)
async def get_workflow_input_schema(workflow_id: str, request: Request):
    state = request_runtime_state(request)
    try:
        async with state.runtime_facade.read_transaction():
            return WorkflowInputSchemaResponse.model_validate(
                _projection(request).input_schema(workflow_id)
            )
    except Exception as error:
        raise _error(error) from error


@router.post("/runs", response_model=WorkflowRunAcceptedResponse, status_code=202)
async def start_run(
    body: WorkflowRunStartRequest,
    request: Request,
    command_id: UUID = Header(alias="Idempotency-Key"),
    lease_token: str = Depends(require_control_lease_header),
):
    state = request_runtime_state(request)
    try:
        async with state.runtime_facade.mutation_transaction(lease_token):
            payload = await _projection(request).start(
                command_id,
                body.workflow_id,
                body.input,
            )
            return WorkflowRunAcceptedResponse(commandId=command_id, **payload)
    except Exception as error:
        raise _error(error) from error


@router.get("/runs", response_model=WorkflowRunListResponse)
async def list_runs(request: Request):
    state = request_runtime_state(request)
    try:
        async with state.runtime_facade.read_transaction():
            return WorkflowRunListResponse(runs=_projection(request).list_runs())
    except Exception as error:
        raise _error(error) from error


@router.get("/runs/{run_id}", response_model=WorkflowRunResponse)
async def get_run(run_id: str, request: Request):
    state = request_runtime_state(request)
    try:
        async with state.runtime_facade.read_transaction():
            return WorkflowRunResponse.model_validate(
                _projection(request).get_run(run_id)
            )
    except Exception as error:
        raise _error(error) from error


@router.post(
    "/runs/{run_id}/input",
    response_model=WorkflowRunAcceptedResponse,
    status_code=202,
)
async def provide_run_input(
    run_id: str,
    body: WorkflowRunInputRequest,
    request: Request,
    command_id: UUID = Header(alias="Idempotency-Key"),
    lease_token: str = Depends(require_control_lease_header),
):
    state = request_runtime_state(request)
    try:
        async with state.runtime_facade.mutation_transaction(lease_token):
            payload = await _projection(request).provide_input(
                command_id,
                run_id,
                input_id=body.input_id,
                values=body.values,
            )
            return WorkflowRunAcceptedResponse(commandId=command_id, **payload)
    except Exception as error:
        raise _error(error) from error


@router.post(
    "/runs/{run_id}/resume",
    response_model=WorkflowRunAcceptedResponse,
    status_code=202,
)
async def resume_run(
    run_id: str,
    request: Request,
    command_id: UUID = Header(alias="Idempotency-Key"),
    lease_token: str = Depends(require_control_lease_header),
):
    state = request_runtime_state(request)
    try:
        async with state.runtime_facade.mutation_transaction(lease_token):
            payload = await _projection(request).resume(command_id, run_id)
            return WorkflowRunAcceptedResponse(commandId=command_id, **payload)
    except Exception as error:
        raise _error(error) from error


@router.get("/runs/{run_id}/outputs", response_model=WorkflowOutputListResponse)
async def get_run_outputs(run_id: str, request: Request):
    state = request_runtime_state(request)
    try:
        async with state.runtime_facade.read_transaction():
            return WorkflowOutputListResponse.model_validate(
                _projection(request).get_outputs(run_id)
            )
    except Exception as error:
        raise _error(error) from error


@router.get("/runs/{run_id}/cost", response_model=WorkflowCostResponse)
async def get_run_cost(run_id: str, request: Request):
    state = request_runtime_state(request)
    try:
        async with state.runtime_facade.read_transaction():
            return WorkflowCostResponse.model_validate(
                _projection(request).get_cost(run_id)
            )
    except Exception as error:
        raise _error(error) from error


@router.get("/runs/{run_id}/events", response_model=WorkflowEventListResponse)
async def get_run_events(run_id: str, request: Request):
    state = request_runtime_state(request)
    try:
        async with state.runtime_facade.read_transaction():
            return WorkflowEventListResponse.model_validate(
                _projection(request).get_events(run_id)
            )
    except Exception as error:
        raise _error(error) from error

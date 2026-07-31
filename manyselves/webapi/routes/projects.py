"""Project registry CRUD and transactional runtime activation routes."""

from fastapi import APIRouter, Depends, Request, Response, status

from ...application.control import ControlLeaseRequired
from ...application.errors import RuntimeBusyError, RuntimeNotReadyError
from ...application.project_registry import (
    ActiveProjectMutation,
    InvalidProjectId,
    ProjectAlreadyExists,
    ProjectNotFound,
    ProjectRecord,
    ProjectRegistryError,
)
from ..errors import ApiError
from ..schemas.projects import ProjectListResponse, ProjectRequest, ProjectResponse
from ..security import require_control_lease_header, require_deployment_access

router = APIRouter(prefix="/projects")


def _response(record: ProjectRecord) -> ProjectResponse:
    return ProjectResponse(id=record.id, active=record.active)


def _project_error(error: Exception) -> ApiError:
    if isinstance(error, RuntimeBusyError):
        return ApiError(
            status_code=status.HTTP_409_CONFLICT,
            code=error.code,
            message=str(error),
            retryable=True,
        )
    if isinstance(error, RuntimeNotReadyError):
        return ApiError(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            code=error.code,
            message=str(error),
            retryable=True,
        )
    if isinstance(error, ControlLeaseRequired):
        return ApiError(
            status_code=status.HTTP_423_LOCKED,
            code=error.code,
            message=str(error),
            retryable=False,
        )
    if isinstance(error, ProjectNotFound):
        code = status.HTTP_404_NOT_FOUND
    elif isinstance(error, InvalidProjectId):
        code = status.HTTP_400_BAD_REQUEST
    elif isinstance(error, (ProjectAlreadyExists, ActiveProjectMutation)):
        code = status.HTTP_409_CONFLICT
    else:
        raise error
    assert isinstance(error, ProjectRegistryError)
    return ApiError(
        status_code=code,
        code=error.code,
        message=str(error),
        retryable=False,
    )


@router.get("", response_model=ProjectListResponse)
async def list_projects(request: Request) -> ProjectListResponse:
    facade = request.app.state.runtime_facade
    registry = request.app.state.project_registry
    async with facade.read_transaction():
        return ProjectListResponse(projects=[_response(item) for item in registry.list()])


@router.post("", response_model=ProjectResponse, status_code=status.HTTP_201_CREATED)
async def create_project(
    body: ProjectRequest,
    request: Request,
    _access: None = Depends(require_deployment_access),
    lease_token: str = Depends(require_control_lease_header),
) -> ProjectResponse:
    facade = request.app.state.runtime_facade
    registry = request.app.state.project_registry
    try:
        async with facade.mutation_transaction(lease_token):
            return _response(registry.create(body.project_id))
    except (ControlLeaseRequired, RuntimeNotReadyError, ProjectRegistryError) as error:
        raise _project_error(error) from error


@router.patch("/{project_id}", response_model=ProjectResponse)
async def rename_project(
    project_id: str,
    body: ProjectRequest,
    request: Request,
    _access: None = Depends(require_deployment_access),
    lease_token: str = Depends(require_control_lease_header),
) -> ProjectResponse:
    facade = request.app.state.runtime_facade
    registry = request.app.state.project_registry
    try:
        async with facade.mutation_transaction(lease_token):
            return _response(registry.rename(project_id, body.project_id))
    except (ControlLeaseRequired, RuntimeNotReadyError, ProjectRegistryError) as error:
        raise _project_error(error) from error


@router.delete("/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_project(
    project_id: str,
    request: Request,
    _access: None = Depends(require_deployment_access),
    lease_token: str = Depends(require_control_lease_header),
) -> Response:
    facade = request.app.state.runtime_facade
    registry = request.app.state.project_registry
    try:
        async with facade.mutation_transaction(lease_token):
            registry.delete(project_id)
    except (ControlLeaseRequired, RuntimeNotReadyError, ProjectRegistryError) as error:
        raise _project_error(error) from error
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{project_id}/activate", response_model=ProjectResponse)
async def activate_project(
    project_id: str,
    request: Request,
    _access: None = Depends(require_deployment_access),
    lease_token: str = Depends(require_control_lease_header),
) -> ProjectResponse:
    facade = request.app.state.runtime_facade
    registry = request.app.state.project_registry
    try:
        project_root = registry.project_root(project_id)
        await facade.activate_workspace(project_root, lease_token)
        record = registry.activate(project_id)
        request.app.state.web_settings.initial_project_id = record.id
        return _response(record)
    except (
        ControlLeaseRequired,
        RuntimeBusyError,
        RuntimeNotReadyError,
        ProjectRegistryError,
    ) as error:
        raise _project_error(error) from error

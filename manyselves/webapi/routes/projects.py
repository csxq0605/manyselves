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
    settings = request.app.state.web_settings
    previous_state: tuple[str, str] | None = None
    target_workspace = None
    try:
        def resolve_workspace():
            nonlocal previous_state, target_workspace
            if (
                request.app.state.maintenance_service.pending_work
                or request.app.state.reporting_facade.active
                or request.app.state.python_run_service.active
            ):
                raise RuntimeBusyError()
            workspace = registry.project_root(project_id)
            previous_state = (registry.active_project_id, settings.initial_project_id)
            target_workspace = workspace
            return workspace

        def commit_activation() -> ProjectResponse:
            assert target_workspace is not None
            record = registry.activate(project_id)
            settings.initial_project_id = record.id
            request.app.state.conversation_service.rebind(target_workspace)
            request.app.state.reporting_facade.rebind(
                request.app.state.runtime_host, target_workspace
            )
            request.app.state.python_run_service.rebind(target_workspace)
            return _response(record)

        def rollback_activation() -> None:
            assert previous_state is not None
            registry.restore_active(previous_state[0])
            settings.initial_project_id = previous_state[1]
            previous_workspace = registry.project_root(previous_state[0])
            request.app.state.conversation_service.rebind(previous_workspace)
            request.app.state.reporting_facade.rebind(
                request.app.state.runtime_host, previous_workspace
            )
            request.app.state.python_run_service.rebind(previous_workspace)

        def reconcile_activation(workspace) -> None:
            actual_project_id = workspace.name
            if registry.project_root(actual_project_id) != workspace.resolve():
                raise InvalidProjectId()
            registry.restore_active(actual_project_id)
            settings.initial_project_id = actual_project_id
            request.app.state.conversation_service.rebind(workspace)
            request.app.state.reporting_facade.rebind(
                request.app.state.runtime_host, workspace
            )
            request.app.state.python_run_service.rebind(workspace)

        return await facade.activate_workspace(
            lease_token=lease_token,
            resolve_workspace=resolve_workspace,
            commit=commit_activation,
            rollback=rollback_activation,
            reconcile=reconcile_activation,
        )
    except (
        ControlLeaseRequired,
        RuntimeBusyError,
        RuntimeNotReadyError,
        ProjectRegistryError,
    ) as error:
        raise _project_error(error) from error

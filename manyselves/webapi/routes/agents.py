"""Typed RuntimeFacade Agent commands."""

from uuid import UUID

from fastapi import APIRouter, Depends, Header, Request, status

from ...application.control import ControlLeaseRequired
from ...application.conversation_service import (
    ConversationNotFoundError,
    ConversationProjectMismatchError,
)
from ...application.errors import (
    AgentNotFoundError,
    CheckpointNotFoundError,
    CommandIdConflictError,
    MaintenanceQuiescedError,
    RollbackPreflightUnsupportedError,
    RuntimeBusyError,
    RuntimeConsistencyFailedError,
    RuntimeNotReadyError,
)
from ...application.models import (
    EditResendCommand,
    InterruptCommand,
    RollbackCommand,
    SendFileContextCommand,
    SendMessageCommand,
)
from ..errors import ApiError
from ..events.sanitizer import EventPayloadSanitizer
from ..schemas.agents import (
    AcceptedCommandResponse,
    AgentDebugEntry,
    AgentDebugResponse,
    AgentDebugUpdate,
    AgentListResponse,
    AgentSnapshot,
    EditResendRequest,
    FileContextRequest,
    RollbackRequest,
    RollbackResponse,
    SendMessageRequest,
)
from ..security import require_authenticated_session, require_control_lease_header

router = APIRouter(prefix="/agents", dependencies=[Depends(require_authenticated_session)])


def _project_id(request: Request, requested: str | None) -> str:
    return requested or request.app.state.project_registry.active_project_id


def _error(error: Exception) -> ApiError:
    if isinstance(error, ConversationProjectMismatchError):
        return ApiError(
            status_code=409,
            code=error.code,
            message="Conversation does not belong to the requested project",
            retryable=False,
        )
    if isinstance(error, AgentNotFoundError):
        return ApiError(status_code=404, code=error.code, message=str(error), retryable=False)
    if isinstance(error, CheckpointNotFoundError):
        return ApiError(status_code=404, code=error.code, message=str(error), retryable=False)
    if isinstance(error, ConversationNotFoundError):
        return ApiError(
            status_code=404,
            code=error.code,
            message="Conversation message was not found",
            retryable=False,
        )
    if isinstance(error, CommandIdConflictError):
        return ApiError(status_code=409, code=error.code, message=str(error), retryable=False)
    if isinstance(error, ControlLeaseRequired):
        return ApiError(status_code=423, code=error.code, message=str(error), retryable=False)
    if isinstance(error, RuntimeNotReadyError):
        return ApiError(status_code=503, code=error.code, message=str(error), retryable=True)
    if isinstance(error, RuntimeConsistencyFailedError):
        return ApiError(status_code=500, code=error.code, message=str(error), retryable=False)
    if isinstance(error, RollbackPreflightUnsupportedError):
        return ApiError(status_code=501, code=error.code, message=str(error), retryable=False)
    if isinstance(error, RuntimeBusyError):
        return ApiError(
            status_code=409, code=error.code, message="Runtime has active work", retryable=True
        )
    if isinstance(error, MaintenanceQuiescedError):
        return ApiError(status_code=409, code=error.code, message=str(error), retryable=True)
    raise error


@router.get("", response_model=AgentListResponse)
async def list_agents(request: Request):
    facade = request.app.state.runtime_facade
    async with facade.read_transaction():
        snapshot = facade.snapshot()
        manager = request.app.state.runtime_host.loop_manager
        return AgentListResponse(
            agents=[
                AgentSnapshot(
                    id=agent_id,
                    status=agent_status,
                    sessionId=manager.get_agent_session_id(agent_id)
                    if manager is not None
                    else None,
                )
                for agent_id, agent_status in snapshot.agent_statuses.items()
            ]
        )


def _debug_response(request: Request, agent_id: str) -> AgentDebugResponse:
    manager = request.app.state.runtime_host.loop_manager
    if manager is None or manager.get_loop(agent_id) is None:
        raise AgentNotFoundError(agent_id)
    entries = request.app.state.runtime_facade.state_projection.debug_for(agent_id)
    sanitizer = EventPayloadSanitizer()
    return AgentDebugResponse(
        agentId=agent_id,
        enabled=manager.get_agent_debug_mode(agent_id),
        entries=[
            AgentDebugEntry(
                model=item.model,
                tokensIn=item.tokens_in,
                tokensOut=item.tokens_out,
                durationMs=item.duration_ms,
                status=item.status,
                timestamp=item.timestamp.isoformat(),
                error=sanitizer.sanitize_field("error", item.error),
            )
            for item in entries
        ],
    )


@router.get("/{agent_id}/debug", response_model=AgentDebugResponse)
async def get_agent_debug(agent_id: str, request: Request) -> AgentDebugResponse:
    facade = request.app.state.runtime_facade
    try:
        async with facade.read_transaction():
            return _debug_response(request, agent_id)
    except AgentNotFoundError as error:
        raise _error(error) from error


@router.patch("/{agent_id}/debug", response_model=AgentDebugResponse)
async def update_agent_debug(
    agent_id: str,
    body: AgentDebugUpdate,
    request: Request,
    lease_token: str = Depends(require_control_lease_header),
) -> AgentDebugResponse:
    try:
        async with request.app.state.runtime_facade.mutation_transaction(lease_token):
            manager = request.app.state.runtime_host.loop_manager
            if manager is None or manager.get_loop(agent_id) is None:
                raise AgentNotFoundError(agent_id)
            request.app.state.runtime_host.backend.set_agent_debug_mode(agent_id, body.enabled)
            return _debug_response(request, agent_id)
    except (
        AgentNotFoundError,
        ControlLeaseRequired,
        RuntimeNotReadyError,
        MaintenanceQuiescedError,
    ) as error:
        raise _error(error) from error


@router.post("/{agent_id}/messages", response_model=AcceptedCommandResponse, status_code=202)
async def send_message(
    agent_id: str,
    body: SendMessageRequest,
    request: Request,
    command_id: UUID = Header(alias="Idempotency-Key"),
    lease_token: str = Depends(require_control_lease_header),
):
    try:
        project_id = _project_id(request, body.project_id)
        request.app.state.conversation_service.require_active_project(
            agent_id,
            project_id,
        )
        return await request.app.state.runtime_facade.send_user_message(
            SendMessageCommand(
                command_id=command_id,
                lease_token=lease_token,
                agent_id=agent_id,
                content=body.content,
                message_id=body.message_id,
                source=body.source,
            )
        )
    except (
        AgentNotFoundError,
        ControlLeaseRequired,
        RuntimeNotReadyError,
        MaintenanceQuiescedError,
        CommandIdConflictError,
        ConversationProjectMismatchError,
    ) as error:
        raise _error(error) from error


@router.post(
    "/{agent_id}/messages/{target_message_id}/edit-resend",
    response_model=AcceptedCommandResponse,
    status_code=202,
)
async def edit_resend(
    agent_id: str,
    target_message_id: str,
    body: EditResendRequest,
    request: Request,
    command_id: UUID = Header(alias="Idempotency-Key"),
    lease_token: str = Depends(require_control_lease_header),
):
    service = request.app.state.conversation_service
    command = EditResendCommand(
        command_id=command_id,
        lease_token=lease_token,
        agent_id=agent_id,
        content=body.content,
        message_id=body.message_id or target_message_id,
        target_message_id=target_message_id,
    )
    try:
        project_id = _project_id(request, body.project_id)
        service.require_active_project(agent_id, project_id)
        return await request.app.state.runtime_facade.edit_resend(
            command,
            prepare=lambda: service.prepare_edit_resend(agent_id, target_message_id),
            restore=service.restore,
        )
    except (
        AgentNotFoundError,
        ControlLeaseRequired,
        RuntimeNotReadyError,
        MaintenanceQuiescedError,
        CommandIdConflictError,
        ConversationNotFoundError,
        RuntimeConsistencyFailedError,
        RuntimeBusyError,
        ConversationProjectMismatchError,
    ) as error:
        raise _error(error) from error


@router.post("/{agent_id}/file-context", response_model=AcceptedCommandResponse, status_code=202)
async def send_file_context(
    agent_id: str,
    body: FileContextRequest,
    request: Request,
    command_id: UUID = Header(alias="Idempotency-Key"),
    lease_token: str = Depends(require_control_lease_header),
):
    try:
        project_id = _project_id(request, body.project_id)
        request.app.state.conversation_service.require_active_project(
            agent_id,
            project_id,
        )
        return await request.app.state.runtime_facade.send_file_context(
            SendFileContextCommand(
                command_id=command_id,
                lease_token=lease_token,
                agent_id=agent_id,
                file_context=body.model_dump(
                    by_alias=False,
                    exclude={"project_id"},
                ),
            )
        )
    except (
        AgentNotFoundError,
        ControlLeaseRequired,
        RuntimeNotReadyError,
        MaintenanceQuiescedError,
        CommandIdConflictError,
        ConversationProjectMismatchError,
    ) as error:
        raise _error(error) from error


@router.post("/{agent_id}/interrupt", response_model=AcceptedCommandResponse, status_code=202)
async def interrupt(
    agent_id: str,
    request: Request,
    command_id: UUID = Header(alias="Idempotency-Key"),
    lease_token: str = Depends(require_control_lease_header),
):
    try:
        return await request.app.state.runtime_facade.interrupt(
            InterruptCommand(command_id=command_id, lease_token=lease_token, agent_id=agent_id)
        )
    except (
        AgentNotFoundError,
        ControlLeaseRequired,
        RuntimeNotReadyError,
        MaintenanceQuiescedError,
        CommandIdConflictError,
    ) as error:
        raise _error(error) from error


@router.post(
    "/{agent_id}/rollback", response_model=RollbackResponse, status_code=status.HTTP_202_ACCEPTED
)
async def rollback(
    agent_id: str,
    body: RollbackRequest,
    request: Request,
    command_id: UUID = Header(alias="Idempotency-Key"),
    lease_token: str = Depends(require_control_lease_header),
):
    service = request.app.state.conversation_service
    try:
        project_id = _project_id(request, body.project_id)
        service.require_active_project(agent_id, project_id)
        result = await request.app.state.runtime_facade.rollback(
            RollbackCommand(
                command_id=command_id,
                lease_token=lease_token,
                agent_id=agent_id,
                checkpoint_id=body.checkpoint_id,
            ),
            before_restore=lambda: service.prepare_rollback(agent_id, body.target_message_id),
            after_restore=lambda restored, _snapshot: service.apply_rollback(
                agent_id, body.target_message_id, restored.conversation_history
            ),
            restore=service.restore,
        )
        return RollbackResponse(
            commandId=command_id,
            restoredFiles=result.restored_files,
            conversationHistory=result.conversation_history,
        )
    except (
        AgentNotFoundError,
        CheckpointNotFoundError,
        ControlLeaseRequired,
        RuntimeNotReadyError,
        MaintenanceQuiescedError,
        CommandIdConflictError,
        ConversationNotFoundError,
        RuntimeConsistencyFailedError,
        RuntimeBusyError,
        RollbackPreflightUnsupportedError,
        ConversationProjectMismatchError,
    ) as error:
        raise _error(error) from error

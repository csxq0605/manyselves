"""Conversation resource routes."""

from fastapi import APIRouter, Depends, Query, Request, status

from ...application.control import ControlLeaseRequired
from ...application.conversation_service import (
    ConversationInvalidError,
    ConversationNotFoundError,
    ConversationProjectMismatchError,
)
from ...application.errors import (
    MaintenanceQuiescedError,
    RuntimeBusyError,
    RuntimeConsistencyFailedError,
    RuntimeNotReadyError,
)
from ..errors import ApiError
from ..schemas.conversations import (
    ConversationActiveSessionResponse,
    ConversationCreateRequest,
    ConversationListResponse,
    ConversationMessagesResponse,
    ConversationRenameRequest,
    ConversationResponse,
)
from ..security import require_authenticated_session, require_control_lease_header
from ..tenant_runtime import request_runtime_state

router = APIRouter(prefix="/conversations", dependencies=[Depends(require_authenticated_session)])


def _project_id(request: Request, requested: str | None) -> str:
    return requested or request_runtime_state(request).project_registry.active_project_id


def _response(item: dict) -> ConversationResponse:
    return ConversationResponse(
        sessionId=item["id"],
        projectId=item["projectId"],
        name=item["name"],
        timestamp=item["timestamp"],
        preview=item.get("preview", ""),
        active=bool(item.get("active")),
    )


def _error(error: Exception) -> ApiError:
    if isinstance(error, ConversationProjectMismatchError):
        return ApiError(
            status_code=409,
            code=error.code,
            message="Conversation does not belong to the requested project",
            retryable=False,
        )
    if isinstance(error, ConversationNotFoundError):
        return ApiError(
            status_code=404, code=error.code, message="Conversation was not found", retryable=False
        )
    if isinstance(error, ConversationInvalidError):
        return ApiError(status_code=422, code=error.code, message=str(error), retryable=False)
    if isinstance(error, ControlLeaseRequired):
        return ApiError(status_code=423, code=error.code, message=str(error), retryable=False)
    if isinstance(error, RuntimeNotReadyError):
        return ApiError(status_code=503, code=error.code, message=str(error), retryable=True)
    if isinstance(error, RuntimeConsistencyFailedError):
        return ApiError(status_code=500, code=error.code, message=str(error), retryable=False)
    if isinstance(error, RuntimeBusyError):
        return ApiError(
            status_code=409, code=error.code, message="Runtime has active work", retryable=True
        )
    if isinstance(error, MaintenanceQuiescedError):
        return ApiError(status_code=409, code=error.code, message=str(error), retryable=True)
    raise error


@router.get("", response_model=ConversationListResponse)
async def list_conversations(
    request: Request,
    project_id: str | None = Query(default=None, alias="projectId", min_length=1),
    agent_id: str = Query("main", alias="agentId"),
):
    state = request_runtime_state(request)
    facade = state.runtime_facade
    service = state.conversation_service
    try:
        async with facade.read_transaction():
            bound_project_id = _project_id(request, project_id)
            items, active = service.list(agent_id, project_id=bound_project_id)
            return ConversationListResponse(
                projectId=bound_project_id,
                conversations=[
                    _response({**item, "active": item.get("id") == active}) for item in items
                ],
                activeSessionId=active,
            )
    except ConversationProjectMismatchError as error:
        raise _error(error) from error


@router.get("/messages", response_model=ConversationMessagesResponse)
async def messages(
    request: Request,
    project_id: str | None = Query(default=None, alias="projectId", min_length=1),
    agent_id: str = Query("main", alias="agentId"),
):
    state = request_runtime_state(request)
    facade = state.runtime_facade
    service = state.conversation_service
    try:
        async with facade.read_transaction():
            bound_project_id = _project_id(request, project_id)
            service.require_active_project(agent_id, bound_project_id)
            return ConversationMessagesResponse(
                sessionId=service.store.get_current_session_id(agent_id),
                projectId=bound_project_id,
                messages=service.messages(agent_id),
            )
    except ConversationProjectMismatchError as error:
        raise _error(error) from error


@router.post("", response_model=ConversationResponse, status_code=status.HTTP_201_CREATED)
async def create_conversation(
    body: ConversationCreateRequest,
    request: Request,
    lease_token: str = Depends(require_control_lease_header),
):
    try:
        state = request_runtime_state(request)
        async with state.runtime_facade.mutation_transaction(lease_token):
            service = state.conversation_service
            bound_project_id = _project_id(request, body.project_id)
            service.require_project(bound_project_id)
            service.require_switch_safe()
            item = await service.create(
                body.name,
                body.agent_id,
                project_id=bound_project_id,
            )
            return _response(item)
    except (
        ControlLeaseRequired,
        RuntimeNotReadyError,
        RuntimeBusyError,
        RuntimeConsistencyFailedError,
        MaintenanceQuiescedError,
        ConversationInvalidError,
        ConversationProjectMismatchError,
    ) as error:
        raise _error(error) from error


@router.patch("/{session_id}", response_model=ConversationResponse)
async def rename_conversation(
    session_id: str,
    body: ConversationRenameRequest,
    request: Request,
    lease_token: str = Depends(require_control_lease_header),
):
    try:
        state = request_runtime_state(request)
        async with state.runtime_facade.mutation_transaction(lease_token):
            bound_project_id = _project_id(request, body.project_id)
            return _response(
                state.conversation_service.rename(
                    session_id,
                    body.name,
                    body.agent_id,
                    project_id=bound_project_id,
                )
            )
    except (
        ControlLeaseRequired,
        RuntimeNotReadyError,
        MaintenanceQuiescedError,
        ConversationNotFoundError,
        ConversationInvalidError,
        ConversationProjectMismatchError,
    ) as error:
        raise _error(error) from error


@router.post("/{session_id}/activate", response_model=ConversationResponse)
async def activate_conversation(
    session_id: str,
    request: Request,
    project_id: str | None = Query(default=None, alias="projectId", min_length=1),
    agent_id: str = Query("main", alias="agentId"),
    lease_token: str = Depends(require_control_lease_header),
):
    try:
        state = request_runtime_state(request)
        async with state.runtime_facade.mutation_transaction(lease_token):
            service = state.conversation_service
            bound_project_id = _project_id(request, project_id)
            service.require_project(bound_project_id)
            service.require_switch_safe()
            return _response(
                await service.activate(
                    session_id,
                    agent_id,
                    project_id=bound_project_id,
                )
            )
    except (
        ControlLeaseRequired,
        RuntimeNotReadyError,
        RuntimeBusyError,
        RuntimeConsistencyFailedError,
        MaintenanceQuiescedError,
        ConversationNotFoundError,
        ConversationProjectMismatchError,
    ) as error:
        raise _error(error) from error


@router.delete("/{session_id}", response_model=ConversationActiveSessionResponse)
async def delete_conversation(
    session_id: str,
    request: Request,
    project_id: str | None = Query(default=None, alias="projectId", min_length=1),
    agent_id: str = Query("main", alias="agentId"),
    lease_token: str = Depends(require_control_lease_header),
):
    try:
        state = request_runtime_state(request)
        async with state.runtime_facade.mutation_transaction(lease_token):
            service = state.conversation_service
            bound_project_id = _project_id(request, project_id)
            service.require_project(bound_project_id)
            service.require_switch_safe()
            active = await service.delete(
                session_id,
                agent_id,
                project_id=bound_project_id,
            )
            return ConversationActiveSessionResponse(
                projectId=bound_project_id,
                activeSessionId=active,
            )
    except (
        ControlLeaseRequired,
        RuntimeNotReadyError,
        RuntimeBusyError,
        RuntimeConsistencyFailedError,
        MaintenanceQuiescedError,
        ConversationNotFoundError,
        ConversationProjectMismatchError,
    ) as error:
        raise _error(error) from error


@router.post("/clear", response_model=ConversationActiveSessionResponse)
async def clear_conversation(
    request: Request,
    project_id: str | None = Query(default=None, alias="projectId", min_length=1),
    agent_id: str = Query("main", alias="agentId"),
    lease_token: str = Depends(require_control_lease_header),
):
    try:
        state = request_runtime_state(request)
        async with state.runtime_facade.mutation_transaction(lease_token):
            service = state.conversation_service
            bound_project_id = _project_id(request, project_id)
            service.require_project(bound_project_id)
            service.require_switch_safe()
            return ConversationActiveSessionResponse(
                projectId=bound_project_id,
                activeSessionId=await service.clear(
                    agent_id,
                    project_id=bound_project_id,
                ),
            )
    except (
        ControlLeaseRequired,
        RuntimeNotReadyError,
        RuntimeBusyError,
        RuntimeConsistencyFailedError,
        MaintenanceQuiescedError,
        ConversationProjectMismatchError,
    ) as error:
        raise _error(error) from error

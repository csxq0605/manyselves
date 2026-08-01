"""Conversation resource routes."""

from fastapi import APIRouter, Depends, Query, Request, status

from ...application.control import ControlLeaseRequired
from ...application.conversation_service import (
    ConversationInvalidError,
    ConversationNotFoundError,
)
from ...application.errors import MaintenanceQuiescedError, RuntimeNotReadyError
from ..errors import ApiError
from ..schemas.conversations import (
    ConversationCreateRequest,
    ConversationListResponse,
    ConversationMessagesResponse,
    ConversationRenameRequest,
    ConversationResponse,
)
from ..security import require_control_lease_header, require_deployment_access

router = APIRouter(prefix="/conversations")


def _response(item: dict) -> ConversationResponse:
    return ConversationResponse(
        sessionId=item["id"],
        name=item["name"],
        timestamp=item["timestamp"],
        preview=item.get("preview", ""),
        active=bool(item.get("active")),
    )


def _error(error: Exception) -> ApiError:
    if isinstance(error, ConversationNotFoundError):
        return ApiError(status_code=404, code=error.code, message="Conversation was not found", retryable=False)
    if isinstance(error, ConversationInvalidError):
        return ApiError(status_code=422, code=error.code, message=str(error), retryable=False)
    if isinstance(error, ControlLeaseRequired):
        return ApiError(status_code=423, code=error.code, message=str(error), retryable=False)
    if isinstance(error, RuntimeNotReadyError):
        return ApiError(status_code=503, code=error.code, message=str(error), retryable=True)
    if isinstance(error, MaintenanceQuiescedError):
        return ApiError(status_code=409, code=error.code, message=str(error), retryable=True)
    raise error


@router.get("", response_model=ConversationListResponse)
async def list_conversations(request: Request, agent_id: str = Query("main", alias="agentId")):
    facade = request.app.state.runtime_facade
    service = request.app.state.conversation_service
    async with facade.read_transaction():
        items, active = service.list(agent_id)
        return ConversationListResponse(
            conversations=[_response({**item, "active": item.get("id") == active}) for item in items],
            activeSessionId=active,
        )


@router.get("/messages", response_model=ConversationMessagesResponse)
async def messages(request: Request, agent_id: str = Query("main", alias="agentId")):
    facade = request.app.state.runtime_facade
    service = request.app.state.conversation_service
    async with facade.read_transaction():
        return ConversationMessagesResponse(
            sessionId=service.store.get_current_session_id(agent_id),
            messages=service.messages(agent_id),
        )


@router.post("", response_model=ConversationResponse, status_code=status.HTTP_201_CREATED)
async def create_conversation(
    body: ConversationCreateRequest,
    request: Request,
    _access: None = Depends(require_deployment_access),
    lease_token: str = Depends(require_control_lease_header),
):
    try:
        async with request.app.state.runtime_facade.mutation_transaction(lease_token):
            item = request.app.state.conversation_service.create(body.name, body.agent_id)
            await request.app.state.conversation_service._sync(  # noqa: SLF001
                body.agent_id, clear_pending=True
            )
            return _response(item)
    except (ControlLeaseRequired, RuntimeNotReadyError, MaintenanceQuiescedError, ConversationInvalidError) as error:
        raise _error(error) from error


@router.patch("/{session_id}", response_model=ConversationResponse)
async def rename_conversation(
    session_id: str,
    body: ConversationRenameRequest,
    request: Request,
    _access: None = Depends(require_deployment_access),
    lease_token: str = Depends(require_control_lease_header),
):
    try:
        async with request.app.state.runtime_facade.mutation_transaction(lease_token):
            return _response(request.app.state.conversation_service.rename(session_id, body.name, body.agent_id))
    except (ControlLeaseRequired, RuntimeNotReadyError, MaintenanceQuiescedError, ConversationNotFoundError, ConversationInvalidError) as error:
        raise _error(error) from error


@router.post("/{session_id}/activate", response_model=ConversationResponse)
async def activate_conversation(
    session_id: str,
    request: Request,
    agent_id: str = Query("main", alias="agentId"),
    _access: None = Depends(require_deployment_access),
    lease_token: str = Depends(require_control_lease_header),
):
    try:
        async with request.app.state.runtime_facade.mutation_transaction(lease_token):
            return _response(await request.app.state.conversation_service.activate(session_id, agent_id))
    except (ControlLeaseRequired, RuntimeNotReadyError, MaintenanceQuiescedError, ConversationNotFoundError) as error:
        raise _error(error) from error


@router.delete("/{session_id}")
async def delete_conversation(
    session_id: str,
    request: Request,
    agent_id: str = Query("main", alias="agentId"),
    _access: None = Depends(require_deployment_access),
    lease_token: str = Depends(require_control_lease_header),
):
    try:
        async with request.app.state.runtime_facade.mutation_transaction(lease_token):
            active = await request.app.state.conversation_service.delete(session_id, agent_id)
            return {"activeSessionId": active}
    except (ControlLeaseRequired, RuntimeNotReadyError, MaintenanceQuiescedError, ConversationNotFoundError) as error:
        raise _error(error) from error


@router.post("/clear")
async def clear_conversation(
    request: Request,
    agent_id: str = Query("main", alias="agentId"),
    _access: None = Depends(require_deployment_access),
    lease_token: str = Depends(require_control_lease_header),
):
    try:
        async with request.app.state.runtime_facade.mutation_transaction(lease_token):
            return {"activeSessionId": await request.app.state.conversation_service.clear(agent_id)}
    except (ControlLeaseRequired, RuntimeNotReadyError, MaintenanceQuiescedError) as error:
        raise _error(error) from error

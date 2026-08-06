"""Authenticated recoverable Server-Sent Events endpoint."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, Header, Query, Request, status
from pydantic import BaseModel
from starlette.responses import StreamingResponse

from ..errors import ApiError
from ..events.broker import (
    EventBroker,
    EventBrokerClosedError,
    EventClient,
    EventClientClosed,
)
from ..events.models import EventEnvelope, EventLogEntry, EventLogResponse
from ..events.sanitizer import EventPayloadSanitizer
from ..security import require_authenticated_session

router = APIRouter(prefix="/events", dependencies=[Depends(require_authenticated_session)])
SSE_HEARTBEAT_SECONDS = 15.0
_LOG_MESSAGE_FIELDS = (
    "message", "content", "brief", "description", "status", "action", "reason", "name", "tool_name",
)
_LOG_MESSAGE_LIMIT = 500


def _frame(event: EventEnvelope) -> str:
    return f"id: {event.event_id}\nevent: {event.type}\ndata: {event.to_json()}\n\n"


def _log_level(event: EventEnvelope) -> str:
    event_type = event.type.casefold()
    status_value = event.payload.get("status")
    payload_status = status_value.casefold() if isinstance(status_value, str) else ""
    if "error" in event_type or "failed" in event_type or payload_status in {"error", "failed"}:
        return "error"
    if any(marker in event_type for marker in ("warning", "waiting", "interrupt", "cancel")):
        return "warning"
    return "info"


def _log_message(event: EventEnvelope) -> str:
    sanitized = EventPayloadSanitizer().sanitize_mapping(event.payload)
    for field in _LOG_MESSAGE_FIELDS:
        value = sanitized.get(field)
        if isinstance(value, str) and value.strip():
            return value.strip()[:_LOG_MESSAGE_LIMIT]
    return event.type


def _log_entry(event: EventEnvelope) -> EventLogEntry:
    return EventLogEntry(
        eventId=event.event_id,
        timestamp=event.timestamp,
        level=_log_level(event),
        type=event.type,
        message=_log_message(event),
        agentId=event.agent_id,
        sessionId=event.session_id,
    )


class _EventStreamBody(AsyncIterator[str]):
    """Own registration independently of whether response iteration ever starts."""

    def __init__(self, request: Request, broker: EventBroker, client: EventClient) -> None:
        self._request = request
        self._broker = broker
        self._client = client
        self._done = False
        self._close_task: asyncio.Task[None] | None = None
        self._read_task: asyncio.Task[EventEnvelope] | None = None

    def __aiter__(self) -> _EventStreamBody:
        return self

    async def __anext__(self) -> str:
        if self._done:
            raise StopAsyncIteration
        try:
            disconnected = await self._request.is_disconnected()
            if self._done:
                raise StopAsyncIteration
            if disconnected:
                await self.aclose()
                raise StopAsyncIteration
            read_task = asyncio.create_task(self._client.get())
            self._read_task = read_task
            try:
                event = await asyncio.wait_for(
                    read_task,
                    timeout=SSE_HEARTBEAT_SECONDS,
                )
            except TimeoutError:
                if self._done:
                    raise StopAsyncIteration from None
                return ": heartbeat\n\n"
            except EventClientClosed:
                await self.aclose()
                raise StopAsyncIteration from None
            finally:
                if self._read_task is read_task:
                    self._read_task = None
            if self._done:
                raise StopAsyncIteration
            frame = _frame(event)
            if event.type == "stream.resync_required":
                await self.aclose()
            return frame
        except asyncio.CancelledError:
            current = asyncio.current_task()
            caller_cancelled = current is not None and current.cancelling() > 0
            await self.aclose()
            if caller_cancelled:
                raise
            if self._done:
                raise StopAsyncIteration from None
            raise
        except BaseException:
            await self.aclose()
            raise

    async def aclose(self) -> None:
        self._done = True
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close_owned())
        task = self._close_task
        caller_cancelled = False
        while not task.done():
            current = asyncio.current_task()
            cancellation_count = current.cancelling() if current is not None else 0
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                updated_count = current.cancelling() if current is not None else 0
                if updated_count > cancellation_count:
                    caller_cancelled = True
                    while current is not None and current.cancelling() > cancellation_count:
                        current.uncancel()
                elif task.done():
                    break
        task.result()
        if caller_cancelled:
            raise asyncio.CancelledError

    async def _close_owned(self) -> None:
        read_task = self._read_task
        if read_task is not None and not read_task.done():
            read_task.cancel()
            try:
                await read_task
            except asyncio.CancelledError:
                pass
        await self._broker.unregister(self._client)


@router.get("/logs", response_model=EventLogResponse)
async def list_event_logs(
    request: Request,
    project_id: str = Query(alias="projectId", min_length=1, max_length=128),
    limit: int = Query(default=200, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> EventLogResponse:
    """Return a bounded project event projection with pagination support."""
    # Try SQLite EventStore first (if available)
    event_store = getattr(request.app.state, "event_store", None)
    if event_store is not None:
        events, total = event_store.list(project_id, limit=limit, offset=offset)
        entries = [_event_log_entry(event) for event in events]
        return EventLogResponse(projectId=project_id, entries=entries, total=total)

    # Fallback to in-memory replay buffer
    broker = getattr(request.app.state, "event_broker", None)
    if broker is None:
        raise ApiError(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            code="EVENT_STREAM_NOT_READY",
            message="Event stream is not ready",
            retryable=True,
        )
    matching = tuple(
        event for event in broker.replay.snapshot() if event.project_id == project_id
    )
    total = len(matching)
    # Apply offset and limit (reversed for newest-first)
    start = max(0, total - offset - limit)
    end = total - offset
    entries = tuple(_log_entry(event) for event in reversed(matching[start:end]))
    return EventLogResponse(projectId=project_id, entries=entries, total=total)


def _event_log_entry(event: dict) -> EventLogEntry:
    """Convert dict event to EventLogEntry."""
    return EventLogEntry(
        eventId=event["eventId"],
        projectId=event["projectId"],
        agentId=event.get("agentId"),
        sessionId=event.get("sessionId"),
        timestamp=event["timestamp"],
        type=event["type"],
        level=event["level"],
        message=event.get("message"),
    )


class EventLogStats(BaseModel):
    """Event log statistics."""

    total: int
    by_level: dict[str, int]
    by_type: dict[str, int]
    date_range: dict[str, str | None]


@router.get("/search")
async def search_event_logs(
    request: Request,
    project_id: str = Query(alias="projectId", min_length=1, max_length=128),
    q: str = Query(min_length=1, max_length=200),
    limit: int = Query(default=100, ge=1, le=500),
) -> list[EventLogEntry]:
    """Search event logs by message content."""
    event_store = getattr(request.app.state, "event_store", None)
    if event_store is None:
        raise ApiError(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            code="EVENT_STORE_NOT_READY",
            message="Event store is not ready",
            retryable=True,
        )

    events = event_store.search(project_id, q, limit=limit)
    return [_event_log_entry(event) for event in events]


@router.get("/stats", response_model=EventLogStats)
async def get_event_log_stats(
    request: Request,
    project_id: str = Query(alias="projectId", min_length=1, max_length=128),
) -> EventLogStats:
    """Get event log statistics for a project."""
    event_store = getattr(request.app.state, "event_store", None)
    if event_store is None:
        raise ApiError(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            code="EVENT_STORE_NOT_READY",
            message="Event store is not ready",
            retryable=True,
        )

    stats = event_store.stats(project_id)
    return EventLogStats(
        total=stats["total"],
        by_level=stats["byLevel"],
        by_type=stats["byType"],
        date_range=stats["dateRange"],
    )


@router.get(
    "",
    response_model=EventEnvelope,
    responses={
        200: {
            "description": "Recoverable runtime event stream",
            "content": {
                "text/event-stream": {
                    "schema": {"type": "string"},
                    "x-event-envelope": {
                        "$ref": "#/components/schemas/EventEnvelope"
                    },
                }
            },
        }
    },
)
async def stream_events(
    request: Request,
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
) -> StreamingResponse:
    """Stream runtime notifications with replay or an explicit bootstrap requirement."""
    broker = getattr(request.app.state, "event_broker", None)
    if broker is None:
        raise ApiError(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            code="EVENT_STREAM_NOT_READY",
            message="Event stream is not ready",
            retryable=True,
        )
    try:
        client = await broker.register(last_event_id)
    except EventBrokerClosedError as exc:
        raise ApiError(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            code="EVENT_STREAM_NOT_READY",
            message="Event stream is not ready",
            retryable=True,
        ) from exc
    return StreamingResponse(
        _EventStreamBody(request, broker, client),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )

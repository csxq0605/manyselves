"""Authenticated recoverable Server-Sent Events endpoint."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, Header, Request, status
from starlette.responses import StreamingResponse

from ..errors import ApiError
from ..events.broker import (
    EventBroker,
    EventBrokerClosedError,
    EventClient,
    EventClientClosed,
)
from ..events.models import EventEnvelope
from ..security import require_authenticated_session
from ..session_auth import SessionPrincipal

router = APIRouter(prefix="/events", dependencies=[Depends(require_authenticated_session)])
SSE_HEARTBEAT_SECONDS = 15.0


def _frame(event: EventEnvelope) -> str:
    return f"id: {event.event_id}\nevent: {event.type}\ndata: {event.to_json()}\n\n"


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
    _access: SessionPrincipal = Depends(require_authenticated_session),
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

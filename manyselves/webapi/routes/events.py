"""Authenticated recoverable Server-Sent Events endpoint."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, Header, Request, status
from starlette.responses import StreamingResponse

from ..errors import ApiError
from ..events.broker import EventBroker
from ..events.models import EventEnvelope
from ..security import require_deployment_access

router = APIRouter(prefix="/events")
SSE_HEARTBEAT_SECONDS = 15.0


def _frame(event: EventEnvelope) -> str:
    return f"id: {event.event_id}\nevent: {event.type}\ndata: {event.to_json()}\n\n"


async def _body(request: Request, broker: EventBroker, cursor: str | None) -> AsyncIterator[str]:
    client = await broker.register(cursor)
    try:
        while True:
            if await request.is_disconnected():
                break
            try:
                event = await asyncio.wait_for(
                    client.get(),
                    timeout=SSE_HEARTBEAT_SECONDS,
                )
            except TimeoutError:
                yield ": heartbeat\n\n"
                continue
            yield _frame(event)
            if event.type == "stream.resync_required":
                break
    finally:
        await broker.unregister(client)


@router.get("")
async def stream_events(
    request: Request,
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    _access: None = Depends(require_deployment_access),
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
    return StreamingResponse(
        _body(request, broker, last_event_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )

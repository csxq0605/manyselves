"""Structured errors returned by versioned web API routes."""

from typing import Any

from fastapi import Request
from starlette.responses import JSONResponse

from .schemas.common import ErrorDetail, ErrorEnvelope


class ApiError(Exception):
    """An expected API error that is safe to serialize for a client."""

    def __init__(
        self,
        *,
        status_code: int,
        code: str,
        message: str,
        retryable: bool,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.retryable = retryable
        self.details = {} if details is None else details


async def api_error_handler(request: Request, exc: ApiError) -> JSONResponse:
    """Render expected API failures with the request ID selected by middleware."""
    request_id = getattr(request.state, "request_id", request.headers.get("X-Request-ID", ""))
    envelope = ErrorEnvelope(
        error=ErrorDetail(
            code=exc.code,
            message=exc.message,
            retryable=exc.retryable,
            details=exc.details,
        ),
        request_id=request_id,
    )
    return JSONResponse(status_code=exc.status_code, content=envelope.model_dump(by_alias=True))

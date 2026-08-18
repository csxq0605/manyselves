"""Structured errors returned by versioned web API routes."""

from http import HTTPStatus
from typing import Any
from uuid import uuid4

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
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
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.retryable = retryable
        self.details = {} if details is None else details
        self.headers = _safe_error_headers(headers)


async def api_error_handler(request: Request, exc: ApiError) -> JSONResponse:
    """Render expected API failures with the request ID selected by middleware."""
    return _error_response(
        request,
        status_code=exc.status_code,
        code=exc.code,
        message=exc.message,
        retryable=exc.retryable,
        details=exc.details,
        headers=exc.headers,
    )


async def request_validation_error_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """Return safe validation metadata without echoing submitted request values."""
    issues = [
        {"code": error["type"], "location": list(error["loc"])}
        for error in exc.errors()
    ]
    return _error_response(
        request,
        status_code=HTTPStatus.UNPROCESSABLE_ENTITY,
        code="REQUEST_VALIDATION_FAILED",
        message="Request validation failed",
        retryable=False,
        details={"issues": issues},
    )


async def http_error_handler(
    request: Request, exc: StarletteHTTPException
) -> JSONResponse:
    """Replace framework HTTP exception details with stable API error fields."""
    return _error_response(
        request,
        status_code=exc.status_code,
        code=_http_error_code(exc.status_code),
        message=_http_error_message(exc.status_code),
        retryable=exc.status_code == HTTPStatus.TOO_MANY_REQUESTS or exc.status_code >= 500,
    )


async def internal_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """Hide unexpected exception text behind the stable internal-error contract."""
    return _error_response(
        request,
        status_code=HTTPStatus.INTERNAL_SERVER_ERROR,
        code="INTERNAL_ERROR",
        message="An unexpected server error occurred",
        retryable=False,
    )


def _error_response(
    request: Request,
    *,
    status_code: int,
    code: str,
    message: str,
    retryable: bool,
    details: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    """Build one envelope and preserve its request ID even after a server failure."""
    request_id = getattr(request.state, "request_id", None) or request.headers.get(
        "X-Request-ID", uuid4().hex
    )
    envelope = ErrorEnvelope(
        error=ErrorDetail(
            code=code,
            message=message,
            retryable=retryable,
            details={} if details is None else details,
        ),
        request_id=request_id,
    )
    response_headers = {"X-Request-ID": request_id}
    response_headers.update(_safe_error_headers(headers))
    return JSONResponse(
        status_code=status_code,
        content=envelope.model_dump(by_alias=True),
        headers=response_headers,
    )


def _safe_error_headers(headers: dict[str, str] | None) -> dict[str, str]:
    """Allow only reviewed response headers and reject response-splitting values."""
    if headers is None:
        return {}
    allowed = {"content-range": "Content-Range", "retry-after": "Retry-After"}
    safe: dict[str, str] = {}
    for name, value in headers.items():
        canonical = allowed.get(name.casefold())
        if canonical is None or "\r" in value or "\n" in value:
            raise ValueError("Unsafe API error response header")
        safe[canonical] = value
    return safe


def _http_error_code(status_code: int) -> str:
    """Name recognized HTTP failures without accepting framework-provided detail text."""
    try:
        return f"HTTP_{HTTPStatus(status_code).name}"
    except ValueError:
        return "HTTP_ERROR"


def _http_error_message(status_code: int) -> str:
    """Provide safe messages for framework failures that clients can surface."""
    messages = {
        HTTPStatus.BAD_REQUEST: "The request could not be processed",
        HTTPStatus.UNAUTHORIZED: "Authentication is required",
        HTTPStatus.FORBIDDEN: "Access is forbidden",
        HTTPStatus.NOT_FOUND: "The requested resource was not found",
        HTTPStatus.METHOD_NOT_ALLOWED: "The requested HTTP method is not allowed",
        HTTPStatus.UNPROCESSABLE_ENTITY: "The request could not be processed",
        HTTPStatus.TOO_MANY_REQUESTS: "Too many requests",
        HTTPStatus.SERVICE_UNAVAILABLE: "The service is unavailable",
    }
    return messages.get(status_code, "The request could not be processed")

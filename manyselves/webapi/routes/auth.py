"""Routes that establish, inspect, and clear the local browser session."""

from datetime import UTC, datetime

from fastapi import APIRouter, Request, Response, status

from ..errors import ApiError
from ..schemas.auth import LoginRequest, SessionResponse

SESSION_COOKIE_NAME = "manyselves_session"

router = APIRouter()


def _session_signer(request: Request):
    """Retrieve the signer initialized with the application lifespan."""
    signer = request.app.state.session_signer
    if signer is None:
        raise RuntimeError("Session signer is unavailable before application startup")
    return signer


@router.post("/auth/login", status_code=status.HTTP_204_NO_CONTENT)
async def login(request: LoginRequest, http_request: Request) -> Response:
    """Establish a browser session after validating the configured administrator."""
    settings = http_request.app.state.web_settings
    if settings is None:
        raise RuntimeError("Web settings are unavailable before application startup")
    catalog = http_request.app.state.account_catalog
    account = catalog.authenticate(request.username, request.password.get_secret_value())
    if account is None:
        raise ApiError(
            status_code=status.HTTP_401_UNAUTHORIZED,
            code="AUTH_INVALID",
            message="Username or password is invalid",
            retryable=False,
        )
    manager = http_request.app.state.tenant_runtime_manager
    if manager is not None:
        http_request.state.tenant_runtime = await manager.get_or_start(account.account_id)
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=_session_signer(http_request).issue(account.username, account.account_id),
        max_age=settings.session_ttl_seconds,
        httponly=True,
        samesite="strict",
        secure=settings.session_cookie_secure,
        path="/",
    )
    return response


@router.get("/auth/session", response_model=SessionResponse)
async def session(http_request: Request) -> SessionResponse:
    """Return the authenticated principal represented by the browser cookie."""
    principal = _session_signer(http_request).verify(
        http_request.cookies.get(SESSION_COOKIE_NAME, "")
    )
    catalog = http_request.app.state.account_catalog
    if principal is None or not catalog.matches_principal(
        principal.account_id, principal.username
    ):
        raise ApiError(
            status_code=status.HTTP_401_UNAUTHORIZED,
            code="AUTH_REQUIRED",
            message="Authentication is required",
            retryable=False,
        )
    return SessionResponse(
        authenticated=True,
        username=principal.username,
        expiresAt=datetime.fromtimestamp(principal.expires_at, UTC),
    )


@router.post("/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(http_request: Request) -> Response:
    """Clear the browser session regardless of whether its value is currently valid."""
    settings = http_request.app.state.web_settings
    if settings is None:
        raise RuntimeError("Web settings are unavailable before application startup")
    response = Response(status_code=status.HTTP_204_NO_CONTENT)
    response.delete_cookie(
        key=SESSION_COOKIE_NAME,
        httponly=True,
        samesite="strict",
        secure=settings.session_cookie_secure,
        path="/",
    )
    return response

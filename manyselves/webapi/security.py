"""Session and runtime-control dependencies for business routes."""

from fastapi import Request, Security, status
from fastapi.security import APIKeyHeader

from .errors import ApiError
from .routes.auth import SESSION_COOKIE_NAME
from .session_auth import SessionPrincipal

control_lease_token = APIKeyHeader(
    name="X-Control-Lease-Token",
    auto_error=False,
    scheme_name="ControlLeaseToken",
)


async def require_authenticated_session(request: Request) -> SessionPrincipal:
    """Require a valid browser session for a business API request."""
    value = request.cookies.get(SESSION_COOKIE_NAME)
    signer = request.app.state.session_signer
    principal = None if signer is None else signer.verify(value or "")
    catalog = request.app.state.account_catalog
    if (
        principal is None
        or catalog is None
        or not catalog.matches_principal(principal.account_id, principal.username)
    ):
        raise ApiError(
            status_code=status.HTTP_401_UNAUTHORIZED,
            code="AUTH_REQUIRED",
            message="An authenticated session is required",
            retryable=False,
        )
    manager = request.app.state.tenant_runtime_manager
    if manager is not None:
        request.state.tenant_runtime = await manager.get_or_start(principal.account_id)
    return principal


def require_control_lease_header(
    lease_token: str | None = Security(control_lease_token),
) -> str:
    """Require a non-empty controller token; the facade validates it under its lock."""
    if not lease_token:
        raise ApiError(
            status_code=status.HTTP_423_LOCKED,
            code="CONTROL_LEASE_REQUIRED",
            message="A valid runtime control lease is required",
            retryable=False,
        )
    return lease_token

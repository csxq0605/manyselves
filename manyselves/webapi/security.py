"""Deployment authentication dependencies for mutating routes."""

import secrets

from fastapi import Depends, Security, status
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer

from .dependencies import get_web_settings
from .errors import ApiError
from .settings import WebSettings

deployment_bearer = HTTPBearer(
    auto_error=False,
    scheme_name="DeploymentBearer",
)
control_lease_token = APIKeyHeader(
    name="X-Control-Lease-Token",
    auto_error=False,
    scheme_name="ControlLeaseToken",
)


def _tokens_equal(left: str, right: str) -> bool:
    """Compare arbitrary token text in constant time without logging either value."""
    return secrets.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def require_deployment_access(
    credentials: HTTPAuthorizationCredentials | None = Security(deployment_bearer),
    settings: WebSettings | None = Depends(get_web_settings),
) -> None:
    """Require the configured deployment bearer token for a mutation."""
    if credentials is None or credentials.scheme.casefold() != "bearer":
        raise ApiError(
            status_code=status.HTTP_401_UNAUTHORIZED,
            code="AUTH_REQUIRED",
            message="Bearer access token is required",
            retryable=False,
        )

    token = credentials.credentials
    if not token:
        raise ApiError(
            status_code=status.HTTP_401_UNAUTHORIZED,
            code="AUTH_REQUIRED",
            message="Bearer access token is required",
            retryable=False,
        )

    if settings is None or not _tokens_equal(settings.access_token.get_secret_value(), token):
        raise ApiError(
            status_code=status.HTTP_403_FORBIDDEN,
            code="AUTH_INVALID",
            message="Bearer access token is invalid",
            retryable=False,
        )


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

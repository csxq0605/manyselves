"""Deployment authentication dependencies for mutating routes."""

import secrets

from fastapi import Depends, Header, status

from .dependencies import get_web_settings
from .errors import ApiError
from .settings import WebSettings


def _tokens_equal(left: str, right: str) -> bool:
    """Compare arbitrary token text in constant time without logging either value."""
    return secrets.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


def require_deployment_access(
    authorization: str | None = Header(default=None),
    settings: WebSettings | None = Depends(get_web_settings),
) -> None:
    """Require the configured deployment bearer token for a mutation."""
    if authorization is None:
        raise ApiError(
            status_code=status.HTTP_401_UNAUTHORIZED,
            code="AUTH_REQUIRED",
            message="Bearer access token is required",
            retryable=False,
        )

    scheme, separator, token = authorization.partition(" ")
    if scheme.casefold() != "bearer" or not separator or not token:
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

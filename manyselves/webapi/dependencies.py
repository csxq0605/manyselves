"""FastAPI dependencies and lifespan composition helpers."""

import inspect
from collections.abc import Awaitable, Callable
from typing import Any, cast

from fastapi import Request

from ..application.global_knowledge_service import GlobalKnowledgeService
from ..application.runtime_facade import RuntimeFacade
from ..application.runtime_host import RuntimeHost
from .settings import WebSettings

RuntimeHostProvider = Callable[[], RuntimeHost | Awaitable[RuntimeHost]]


def get_runtime_host(request: Request) -> RuntimeHost | None:
    """Return the runtime host only after the application lifespan initializes it."""
    return cast(RuntimeHost | None, getattr(request.app.state, "runtime_host", None))


def get_runtime_facade(request: Request) -> RuntimeFacade | None:
    """Return the facade owned by the active application lifespan."""
    return cast(RuntimeFacade | None, getattr(request.app.state, "runtime_facade", None))


def get_web_settings(request: Request) -> WebSettings | None:
    """Return settings once startup has validated them."""
    return cast(WebSettings | None, getattr(request.app.state, "web_settings", None))


def get_global_knowledge_service(request: Request) -> GlobalKnowledgeService | None:
    """Return the fixed-root global library only after lifespan initialization."""
    return cast(
        GlobalKnowledgeService | None,
        getattr(request.app.state, "global_knowledge_service", None),
    )


async def resolve_runtime_host(app: Any) -> RuntimeHost:
    """Build the host at lifespan entry, honoring an app-local test override."""
    override = cast(RuntimeHostProvider | None, app.dependency_overrides.get(get_runtime_host))
    if override is None:
        return RuntimeHost.create()

    host = override()
    if inspect.isawaitable(host):
        return await host
    return host

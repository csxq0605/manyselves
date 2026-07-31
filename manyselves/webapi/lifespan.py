"""Application lifecycle ownership for the one shared runtime."""

import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import AsyncIterator

from fastapi import FastAPI

from ..application.control import ControlLeaseService
from ..application.runtime_facade import RuntimeFacade
from .dependencies import resolve_runtime_host
from .settings import WebSettings


@asynccontextmanager
async def application_lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Start and stop exactly one runtime host for this app instance."""
    settings = app.state.web_settings
    if settings is None:
        settings = WebSettings()
        app.state.web_settings = settings

    host = await resolve_runtime_host(app)
    facade = RuntimeFacade(
        host,
        leases=ControlLeaseService(ttl=timedelta(seconds=settings.control_lease_seconds)),
    )
    app.state.runtime_host = host
    app.state.runtime_facade = facade
    app.state.event_broker = None
    app.state.runtime_read_lock = asyncio.Lock()

    try:
        await host.start(settings.data_root / settings.initial_project_id)
    except BaseException:
        await host.stop()
        raise

    try:
        yield
    finally:
        await host.stop()

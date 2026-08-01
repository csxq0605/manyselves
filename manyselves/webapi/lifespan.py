"""Application lifecycle ownership for the one shared runtime."""

from contextlib import asynccontextmanager
from datetime import timedelta
from typing import AsyncIterator

from fastapi import FastAPI
from loguru import logger

from ..application.control import ControlLeaseService
from ..application.conversation_service import ConversationService
from ..application.maintenance_service import MaintenanceService
from ..application.project_registry import ProjectRegistry
from ..application.python_run_service import PythonRunService
from ..application.reporting_facade import ReportingFacade
from ..application.runtime_facade import RuntimeFacade
from ..core.loops.bus import MessageBus
from .dependencies import resolve_runtime_host
from .settings import WebSettings


@asynccontextmanager
async def application_lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Start and stop exactly one runtime host for this app instance."""
    async with app.state.lifecycle_lock:
        if app.state.lifecycle_active:
            raise RuntimeError("Application lifespan is already active")
        app.state.lifecycle_active = True

    host = None
    try:
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
        registry = ProjectRegistry(settings.data_root, settings.initial_project_id)
        registry.ensure_initial()
        app.state.project_registry = registry
        active_workspace = registry.project_root(settings.initial_project_id)
        await host.start(active_workspace)
        bus = getattr(host, "bus", None)
        if bus is None:
            bus = MessageBus()
        conversations = ConversationService(
            active_workspace,
            facade=facade,
            bus=bus,
        )
        reporting = ReportingFacade.from_runtime(host, workspace=active_workspace)
        python_runs = PythonRunService(
            active_workspace,
            bus=bus,
            timeout_seconds=settings.python_timeout_seconds,
            output_limit_bytes=settings.python_output_limit_bytes,
        )
        maintenance = MaintenanceService(
            facade,
            conversations,
            reporting,
            python_runs,
            config_manager=getattr(host, "config_manager", None),
        )
        app.state.conversation_service = conversations
        app.state.reporting_facade = reporting
        app.state.python_run_service = python_runs
        app.state.maintenance_service = maintenance
    except BaseException:
        try:
            if host is not None:
                await host.stop()
        except BaseException:
            logger.exception("Runtime cleanup failed after lifespan startup failure")
        finally:
            async with app.state.lifecycle_lock:
                app.state.lifecycle_active = False
        raise

    try:
        yield
    finally:
        cleanup_error: BaseException | None = None

        async def cleanup(awaitable) -> None:
            nonlocal cleanup_error
            try:
                await awaitable
            except BaseException as error:
                if cleanup_error is None:
                    cleanup_error = error
                else:
                    cleanup_error.add_note(f"Additional shutdown failure: {error!r}")

        async def stop_producers_with_retry(boundary) -> None:
            first_error: BaseException | None = None
            for _attempt in range(2):
                try:
                    await boundary()
                    return
                except BaseException as error:
                    if first_error is None:
                        first_error = error
                    else:
                        first_error.add_note(
                            f"Producer shutdown retry also failed: {error!r}"
                        )
            assert first_error is not None
            raise first_error

        try:
            await cleanup(facade.begin_shutdown())
            stop_producers = getattr(host, "stop_producers", None)
            split_shutdown = callable(stop_producers)
            producers_stopped = True
            if split_shutdown:
                try:
                    await stop_producers_with_retry(stop_producers)
                except BaseException as error:
                    producers_stopped = False
                    if cleanup_error is None:
                        cleanup_error = error
                    else:
                        cleanup_error.add_note(
                            f"Producer shutdown failed: {error!r}"
                        )
            if producers_stopped:
                reporting = getattr(app.state, "reporting_facade", None)
                if reporting is not None:
                    await cleanup(reporting.close())
                python_runs = getattr(app.state, "python_run_service", None)
                if python_runs is not None:
                    await cleanup(python_runs.close())
                conversations = getattr(app.state, "conversation_service", None)
                if conversations is not None:
                    await cleanup(conversations.close())
                if split_shutdown:
                    await cleanup(host.stop_bus())
                else:
                    await cleanup(host.stop())
            if cleanup_error is not None:
                raise cleanup_error
        finally:
            async with app.state.lifecycle_lock:
                app.state.lifecycle_active = False

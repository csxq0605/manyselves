"""Application lifecycle ownership for the one shared runtime."""

import asyncio
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
from .events.broker import EventBroker
from .events.mapper import EventContext
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

        def resolve_event_context(message, sequence: int) -> EventContext:
            registry = app.state.project_registry
            active_conversations = app.state.conversation_service
            agent_id = getattr(message, "agent_type", None)
            if agent_id is None:
                agent_id = getattr(message, "sender", None)
            agent_id = str(agent_id) if agent_id is not None else None
            session_id = None
            manager = getattr(app.state.runtime_host, "loop_manager", None)
            get_session = getattr(manager, "get_agent_session_id", None)
            if agent_id is not None and callable(get_session):
                session_id = get_session(agent_id)
            if agent_id is not None and session_id is None:
                session_id = active_conversations.store._current_session_ids.get(  # noqa: SLF001
                    agent_id
                )
            return EventContext(
                event_id=f"evt-{sequence}",
                sequence=sequence,
                project_id=registry.active_project_id,
                session_id=session_id,
                agent_id=agent_id,
                run_id=(
                    getattr(message, "run_id", None)
                    or getattr(message, "workflow_id", None)
                    or None
                ),
                message_id=getattr(message, "message_id", None),
            )

        broker = EventBroker(
            bus=bus,
            context_resolver=resolve_event_context,
            replay_capacity=settings.sse_replay_capacity,
            client_capacity=settings.sse_client_queue_capacity,
        )
        broker.start()
        app.state.event_broker = broker
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
        async def await_definite(awaitable) -> tuple[BaseException | None, bool]:
            """Obtain an owned cleanup outcome while remembering caller cancellation."""
            task = asyncio.create_task(awaitable)
            caller_cancelled = False
            while not task.done():
                current = asyncio.current_task()
                cancellation_count = current.cancelling() if current is not None else 0
                try:
                    await asyncio.shield(task)
                except asyncio.CancelledError:
                    updated_count = current.cancelling() if current is not None else 0
                    if updated_count > cancellation_count:
                        caller_cancelled = True
                        while current is not None and current.cancelling() > cancellation_count:
                            current.uncancel()
                    elif task.done():
                        break
                except BaseException:
                    break
            try:
                task.result()
            except BaseException as error:
                return error, caller_cancelled
            return None, caller_cancelled

        caller_cancelled = False

        async def require_stage(awaitable) -> None:
            nonlocal caller_cancelled
            error, cancelled = await await_definite(awaitable)
            caller_cancelled = caller_cancelled or cancelled
            if error is not None:
                raise error

        async def stop_producers_with_retry(boundary) -> None:
            nonlocal caller_cancelled
            first_error: BaseException | None = None
            for _attempt in range(2):
                error, cancelled = await await_definite(boundary())
                caller_cancelled = caller_cancelled or cancelled
                if error is None:
                    return
                if first_error is None:
                    first_error = error
                else:
                    first_error.add_note(
                        f"Producer shutdown retry also failed: {error!r}"
                    )
            assert first_error is not None
            raise first_error

        shutdown_error: BaseException | None = None
        try:
            await require_stage(facade.begin_shutdown())
            stop_producers = getattr(host, "stop_producers", None)
            split_shutdown = callable(stop_producers)
            if split_shutdown:
                await stop_producers_with_retry(stop_producers)
            reporting = getattr(app.state, "reporting_facade", None)
            if reporting is not None:
                await require_stage(reporting.close())
            python_runs = getattr(app.state, "python_run_service", None)
            if python_runs is not None:
                await require_stage(python_runs.close())
            conversations = getattr(app.state, "conversation_service", None)
            if conversations is not None:
                await require_stage(conversations.close())
            broker = getattr(app.state, "event_broker", None)
            if broker is not None:
                await require_stage(broker.close())
            if split_shutdown:
                await require_stage(host.stop_bus())
            else:
                await require_stage(host.stop())
        except BaseException as error:
            shutdown_error = error
        finally:
            async def deactivate_lifespan() -> None:
                async with app.state.lifecycle_lock:
                    app.state.lifecycle_active = False

            deactivate_error, cancelled = await await_definite(deactivate_lifespan())
            caller_cancelled = caller_cancelled or cancelled
            if deactivate_error is not None:
                if shutdown_error is None:
                    shutdown_error = deactivate_error
                else:
                    shutdown_error.add_note(
                        f"Lifespan deactivation failed: {deactivate_error!r}"
                    )
        if shutdown_error is not None:
            raise shutdown_error
        if caller_cancelled:
            raise asyncio.CancelledError

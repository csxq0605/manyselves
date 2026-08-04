"""Application lifecycle ownership for the one shared runtime."""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any, AsyncIterator
from uuid import uuid4

from fastapi import FastAPI
from loguru import logger

from ..application.async_ownership import await_owned
from ..application.control import ControlLeaseService
from ..application.conversation_service import ConversationService
from ..application.global_knowledge_service import GlobalKnowledgeService
from ..application.maintenance_service import MaintenanceService
from ..application.project_registry import ProjectRegistry
from ..application.python_run_service import PythonRunService
from ..application.reporting_facade import ReportingFacade
from ..application.runtime_facade import RuntimeFacade
from ..core.loops.bus import MessageBus
from ..interfaces.types import PeerQueryMessage, PeerReplyMessage
from .dependencies import resolve_runtime_host
from .events.broker import EventBroker
from .events.mapper import EventContext
from .session_auth import SessionSigner
from .settings import WebSettings


@dataclass(slots=True)
class LifespanCleanupOwnership:
    """Resources retained until every ordered lifecycle cleanup stage completes."""

    host: Any
    facade: RuntimeFacade | None
    reporting: ReportingFacade | None
    python_runs: PythonRunService | None
    conversations: ConversationService | None
    broker: EventBroker | None
    completed: set[str] = field(default_factory=set)


async def _cleanup_owned_runtime(
    ownership: LifespanCleanupOwnership,
) -> tuple[list[tuple[str, BaseException]], bool]:
    """Release one runtime in dependency order while retaining failed stages."""
    failures: list[tuple[str, BaseException]] = []
    caller_cancelled = False

    async def cleanup(stage: str, name: str, awaitable) -> bool:
        nonlocal caller_cancelled
        outcome = await await_owned(awaitable)
        caller_cancelled = caller_cancelled or outcome.cancellation_requested
        if outcome.error is not None:
            failures.append((name, outcome.error))
            return False
        ownership.completed.add(stage)
        return True

    if "begin_shutdown" not in ownership.completed:
        if ownership.facade is None:
            ownership.completed.add("begin_shutdown")
        elif not await cleanup(
            "begin_shutdown", "shutdown grant", ownership.facade.begin_shutdown()
        ):
            return failures, caller_cancelled

    stop_producers = getattr(ownership.host, "stop_producers", None)
    split_shutdown = callable(stop_producers)
    if "producers" not in ownership.completed:
        if not split_shutdown:
            ownership.completed.add("producers")
        else:
            first_error: BaseException | None = None
            for _attempt in range(2):
                outcome = await await_owned(stop_producers())
                caller_cancelled = caller_cancelled or outcome.cancellation_requested
                if outcome.error is None:
                    ownership.completed.add("producers")
                    break
                if first_error is None:
                    first_error = outcome.error
                else:
                    first_error.add_note("Producer shutdown retry also failed")
            if "producers" not in ownership.completed:
                assert first_error is not None
                failures.append(("producer cleanup", first_error))
                return failures, caller_cancelled

    service_stages = (
        ("reporting", "reporting cleanup", ownership.reporting),
        ("python", "Python cleanup", ownership.python_runs),
        ("conversations", "conversation cleanup", ownership.conversations),
        ("broker", "event broker cleanup", ownership.broker),
    )
    for stage, name, resource in service_stages:
        if stage in ownership.completed:
            continue
        if resource is None:
            ownership.completed.add(stage)
            continue
        await cleanup(stage, name, resource.close())

    if any(stage not in ownership.completed for stage, _, _ in service_stages):
        return failures, caller_cancelled

    if "bus" not in ownership.completed:
        if split_shutdown:
            await cleanup("bus", "bus cleanup", ownership.host.stop_bus())
        else:
            await cleanup("bus", "host cleanup", ownership.host.stop())
    return failures, caller_cancelled


def _lifecycle_ownership(
    *,
    host: Any,
    facade: RuntimeFacade | None,
    reporting: ReportingFacade | None,
    python_runs: PythonRunService | None,
    conversations: ConversationService | None,
    broker: EventBroker | None,
) -> LifespanCleanupOwnership:
    return LifespanCleanupOwnership(
        host=host,
        facade=facade,
        reporting=reporting,
        python_runs=python_runs,
        conversations=conversations,
        broker=broker,
    )


def _expose_lifecycle_ownership(app: FastAPI, ownership: LifespanCleanupOwnership | None) -> None:
    """Keep unfinished cleanup reachable, or clear resources after definite release."""
    app.state.runtime_host = None if ownership is None else ownership.host
    app.state.runtime_facade = None if ownership is None else ownership.facade
    app.state.conversation_service = None if ownership is None else ownership.conversations
    app.state.reporting_facade = None if ownership is None else ownership.reporting
    app.state.python_run_service = None if ownership is None else ownership.python_runs
    app.state.event_broker = None if ownership is None else ownership.broker
    if ownership is None:
        app.state.maintenance_service = None
        app.state.global_knowledge_service = None


@asynccontextmanager
async def application_lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Start and stop exactly one runtime host for this app instance."""
    async with app.state.lifecycle_lock:
        if app.state.lifecycle_active:
            raise RuntimeError("Application lifespan is already active")
        app.state.lifecycle_active = True

    host = None
    facade = None
    conversations = None
    reporting = None
    python_runs = None
    broker = None
    ownership: LifespanCleanupOwnership | None = None
    try:
        pending_ownership = app.state._lifecycle_cleanup_pending
        if pending_ownership is not None:
            pending_failures, pending_cancelled = await _cleanup_owned_runtime(pending_ownership)
            if pending_failures:
                _name, cleanup_error = pending_failures[0]
                retry_error = RuntimeError("Previous lifespan cleanup is incomplete")
                for failure_name, _failure in pending_failures:
                    retry_error.add_note(f"{failure_name} failed")
                if pending_cancelled:
                    retry_error.add_note("Caller cancellation observed during lifecycle cleanup")
                raise retry_error from cleanup_error
            app.state._lifecycle_cleanup_pending = None
            _expose_lifecycle_ownership(app, None)
            if pending_cancelled:
                raise asyncio.CancelledError

        settings = app.state.web_settings
        if settings is None:
            settings = WebSettings()
            app.state.web_settings = settings

        app.state.session_signer = SessionSigner(
            settings.data_root / ".manyselves" / "auth" / "session.key",
            settings.session_ttl_seconds,
        )
        app.state.global_knowledge_service = GlobalKnowledgeService.from_data_root(
            settings.data_root,
            max_text_bytes=settings.text_file_size_limit_bytes,
            max_upload_bytes=settings.upload_size_limit_bytes,
            max_tree_entries=settings.file_tree_entry_limit,
        )

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

        stream_id = uuid4().hex

        def resolve_event_context(message, sequence: int) -> EventContext:
            registry = app.state.project_registry
            active_conversations = app.state.conversation_service
            agent_id = getattr(message, "agent_type", None)
            if agent_id is None:
                agent_id = getattr(message, "sender", None)
            agent_id = str(agent_id) if agent_id is not None else None
            session_id = None
            if type(message) is PeerQueryMessage:
                session_id = message.source_session_id
            elif type(message) is PeerReplyMessage:
                session_id = message.target_session_id
            manager = getattr(app.state.runtime_host, "loop_manager", None)
            get_session = getattr(manager, "get_agent_session_id", None)
            if session_id is None and agent_id is not None and callable(get_session):
                session_id = get_session(agent_id)
            if agent_id is not None and session_id is None:
                session_id = active_conversations.store._current_session_ids.get(  # noqa: SLF001
                    agent_id
                )
            return EventContext(
                event_id=f"{stream_id}:evt-{sequence}",
                stream_id=stream_id,
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
            stream_id=stream_id,
            observer=facade.state_projection.observe,
        )
        broker.start()
        app.state.event_broker = broker
        ownership = _lifecycle_ownership(
            host=host,
            facade=facade,
            reporting=reporting,
            python_runs=python_runs,
            conversations=conversations,
            broker=broker,
        )
    except BaseException as startup_error:
        cleanup_failures: list[tuple[str, BaseException]] = []
        cleanup_cancelled = False
        if host is not None:
            if ownership is None:
                ownership = _lifecycle_ownership(
                    host=host,
                    facade=facade,
                    reporting=reporting,
                    python_runs=python_runs,
                    conversations=conversations,
                    broker=broker,
                )
            app.state._lifecycle_cleanup_pending = ownership
            _expose_lifecycle_ownership(app, ownership)
            cleanup_failures, cleanup_cancelled = await _cleanup_owned_runtime(ownership)
            if cleanup_cancelled:
                startup_error.add_note("Caller cancellation observed during lifecycle cleanup")
            for name, _cleanup_error in cleanup_failures:
                startup_error.add_note(f"{name} failed")
                logger.error("{} after lifespan startup failure", name)
            if not cleanup_failures:
                app.state._lifecycle_cleanup_pending = None
                _expose_lifecycle_ownership(app, None)
        elif app.state._lifecycle_cleanup_pending is None:
            _expose_lifecycle_ownership(app, None)

        async def deactivate_failed_startup() -> None:
            async with app.state.lifecycle_lock:
                app.state.lifecycle_active = False

        deactivate = await await_owned(deactivate_failed_startup())
        if deactivate.cancellation_requested and not cleanup_cancelled:
            startup_error.add_note("Caller cancellation observed during lifecycle cleanup")
        if deactivate.error is not None:
            startup_error.add_note("Lifespan deactivation failed")
        raise

    try:
        yield
    finally:
        assert ownership is not None
        app.state._lifecycle_cleanup_pending = ownership
        _expose_lifecycle_ownership(app, ownership)
        cleanup_failures, caller_cancelled = await _cleanup_owned_runtime(ownership)
        if not cleanup_failures:
            app.state._lifecycle_cleanup_pending = None
            _expose_lifecycle_ownership(app, None)

        async def deactivate_lifespan() -> None:
            async with app.state.lifecycle_lock:
                app.state.lifecycle_active = False

        deactivate = await await_owned(deactivate_lifespan())
        caller_cancelled = caller_cancelled or deactivate.cancellation_requested

        shutdown_error: BaseException | None = None
        if cleanup_failures:
            _, shutdown_error = cleanup_failures[0]
            for name, _cleanup_error in cleanup_failures[1:]:
                shutdown_error.add_note(f"{name} failed")
        if deactivate.error is not None:
            if shutdown_error is None:
                shutdown_error = deactivate.error
            else:
                shutdown_error.add_note("Lifespan deactivation failed")
        if shutdown_error is not None:
            if caller_cancelled:
                shutdown_error.add_note("Caller cancellation observed during lifecycle cleanup")
            raise shutdown_error
        if caller_cancelled:
            raise asyncio.CancelledError

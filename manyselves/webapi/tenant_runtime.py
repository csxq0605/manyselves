"""Account-scoped workers and storage inside one FastAPI service process."""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Awaitable, Callable
from uuid import uuid4

from ..application.control import ControlLeaseService
from ..application.conversation_service import ConversationService
from ..application.global_knowledge_service import GlobalKnowledgeService
from ..application.maintenance_service import MaintenanceService
from ..application.project_registry import ProjectRegistry
from ..application.python_run_service import PythonRunService
from ..application.runtime_facade import RuntimeFacade
from ..application.runtime_host import RuntimeHost
from ..application.runtime_services import build_runtime_services_view
from ..application.workflow_projection import WorkflowProjectionFacade
from ..config import ConfigManager
from ..core.loops.bus import MessageBus
from ..interfaces.types import PeerQueryMessage, PeerReplyMessage
from .accounts import AccountCatalog
from .events.broker import EventBroker
from .events.mapper import EventContext
from .events.store import EventStore
from .settings import WebSettings


@dataclass(slots=True)
class TenantRuntime:
    """All mutable runtime and persistence owners for exactly one account."""

    account_id: str
    data_root: Path
    web_settings: WebSettings
    runtime_host: RuntimeHost
    runtime_facade: RuntimeFacade
    project_registry: ProjectRegistry
    conversation_service: ConversationService
    python_run_service: PythonRunService
    maintenance_service: MaintenanceService
    global_knowledge_service: GlobalKnowledgeService
    event_broker: EventBroker
    event_store: EventStore
    workflow_projection: WorkflowProjectionFacade

    async def close(self) -> None:
        """Stop only this account's producers, services, broker, and bus."""

        await self.runtime_facade.begin_shutdown()
        await self.workflow_projection.close()
        stop_producers = getattr(self.runtime_host, "stop_producers", None)
        if callable(stop_producers):
            await stop_producers()
        await self.python_run_service.close()
        await self.conversation_service.close()
        await self.event_broker.close()
        stop_bus = getattr(self.runtime_host, "stop_bus", None)
        if callable(stop_bus):
            await stop_bus()
        else:
            await self.runtime_host.stop()

    async def rebind_workflow_projection(self, workspace: Path) -> None:
        """Replace project-scoped Capability runtimes after Host activation."""

        previous = self.workflow_projection
        replacement = WorkflowProjectionFacade(
            workspace,
            build_runtime_services_view(self.runtime_host),
        )
        await previous.close()
        self.workflow_projection = replacement


TenantFactory = Callable[[str, Path, WebSettings], Awaitable[TenantRuntime]]


def _attach_event_persistence(broker: EventBroker, event_store: EventStore) -> None:
    original_publish_internal = broker.publish_internal

    async def publish_with_persistence(message) -> None:
        await original_publish_internal(message)
        snapshot = broker.replay.snapshot()
        if snapshot:
            event_store.append(snapshot[-1])

    broker.publish_internal = publish_with_persistence  # type: ignore[method-assign]


def account_data_root(settings: WebSettings, account_id: str) -> Path:
    """Return the non-overlapping write boundary for one account."""

    base = settings.data_root.expanduser().resolve()
    root = (base / "accounts" / account_id).resolve()
    if root.parent != (base / "accounts").resolve():
        raise ValueError("account data root escaped the configured storage root")
    return root


async def start_tenant_runtime(
    account_id: str,
    data_root: Path,
    settings: WebSettings,
) -> TenantRuntime:
    """Create one independent in-process RuntimeHost/worker for an account."""

    data_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name != "nt":
        os.chmod(data_root, 0o700)
    tenant_settings = settings.model_copy(
        update={
            "data_root": data_root,
            "event_db_path": data_root / ".manyselves" / "events.db",
            "initial_project_id": "default",
        }
    )
    config_path = data_root / ".manyselves" / "config" / "manyselves.config.yaml"
    manager = ConfigManager(config_path, inherit_environment=False)
    host = RuntimeHost.create(manager)
    facade = RuntimeFacade(
        host,
        leases=ControlLeaseService(
            ttl=timedelta(seconds=tenant_settings.control_lease_seconds)
        ),
    )
    registry = ProjectRegistry(data_root, tenant_settings.get_initial_project_id())
    registry.ensure_initial()
    workspace = registry.project_root(registry.active_project_id)
    global_knowledge = GlobalKnowledgeService.from_data_root(
        data_root,
        max_text_bytes=tenant_settings.text_file_size_limit_bytes,
        max_upload_bytes=tenant_settings.upload_size_limit_bytes,
        max_tree_entries=tenant_settings.file_tree_entry_limit,
    )
    host.set_global_knowledge_root(global_knowledge.root)
    await host.start(workspace)
    bus = getattr(host, "bus", None) or MessageBus()
    conversations = ConversationService(workspace, facade=facade, bus=bus)
    workflow_projection = WorkflowProjectionFacade(
        workspace,
        build_runtime_services_view(host),
    )
    python_runs = PythonRunService(
        workspace,
        bus=bus,
        timeout_seconds=tenant_settings.python_timeout_seconds,
        output_limit_bytes=tenant_settings.python_output_limit_bytes,
    )
    maintenance = MaintenanceService(
        facade,
        conversations,
        workflow_projection,
        python_runs,
        config_manager=manager,
    )
    stream_id = uuid4().hex

    def resolve_event_context(message, sequence: int) -> EventContext:
        agent_id = getattr(message, "agent_type", None) or getattr(message, "sender", None)
        agent_id = str(agent_id) if agent_id is not None else None
        session_id = None
        if type(message) is PeerQueryMessage:
            session_id = message.source_session_id
        elif type(message) is PeerReplyMessage:
            session_id = message.target_session_id
        get_session = getattr(host.loop_manager, "get_agent_session_id", None)
        if session_id is None and agent_id is not None and callable(get_session):
            session_id = get_session(agent_id)
        if agent_id is not None and session_id is None:
            session_id = conversations.store._current_session_ids.get(agent_id)  # noqa: SLF001
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
        replay_capacity=tenant_settings.sse_replay_capacity,
        client_capacity=tenant_settings.sse_client_queue_capacity,
        stream_id=stream_id,
        observer=facade.state_projection.observe,
    )
    event_store = EventStore(tenant_settings.event_db_path)
    _attach_event_persistence(broker, event_store)
    broker.start()
    return TenantRuntime(
        account_id=account_id,
        data_root=data_root,
        web_settings=tenant_settings,
        runtime_host=host,
        runtime_facade=facade,
        project_registry=registry,
        conversation_service=conversations,
        python_run_service=python_runs,
        maintenance_service=maintenance,
        global_knowledge_service=global_knowledge,
        event_broker=broker,
        event_store=event_store,
        workflow_projection=workflow_projection,
    )


class TenantRuntimeManager:
    """Lazily own one independent worker graph per configured account."""

    def __init__(
        self,
        catalog: AccountCatalog,
        settings: WebSettings,
        *,
        factory: TenantFactory = start_tenant_runtime,
    ) -> None:
        self.catalog = catalog
        self.settings = settings
        self._factory = factory
        self._runtimes: dict[str, TenantRuntime] = {}
        self._locks = {account_id: asyncio.Lock() for account_id in catalog.account_ids}
        self._closed = False

    async def get_or_start(self, account_id: str) -> TenantRuntime:
        if self._closed or not self.catalog.contains(account_id):
            raise KeyError(account_id)
        existing = self._runtimes.get(account_id)
        if existing is not None:
            return existing
        async with self._locks[account_id]:
            existing = self._runtimes.get(account_id)
            if existing is not None:
                return existing
            runtime = await self._factory(
                account_id,
                account_data_root(self.settings, account_id),
                self.settings,
            )
            self._runtimes[account_id] = runtime
            return runtime

    async def close(self) -> None:
        self._closed = True
        failures: list[BaseException] = []
        for runtime in reversed(tuple(self._runtimes.values())):
            try:
                await runtime.close()
            except BaseException as error:
                failures.append(error)
        self._runtimes.clear()
        if failures:
            primary = failures[0]
            for _extra in failures[1:]:
                primary.add_note("another account runtime also failed to close")
            raise primary


def request_runtime_state(request):
    """Resolve account state, retaining the legacy single-account test boundary."""

    request_state = getattr(request, "state", None)
    return getattr(request_state, "tenant_runtime", None) or request.app.state

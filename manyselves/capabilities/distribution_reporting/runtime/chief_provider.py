"""Provider composition for the file-defined initial Chief chapter task.

The Chief lifecycle and typed prompt/result bridge remain Capability-owned in
``chief_runtime``.  This module only supplies the account-scoped Provider
resources and the three Tools declared by ``chief-chapter-edit``.  It reuses
the existing Module tool assembler so the collaboration Tool implementations
have one owner; the prepared TaskDefinition is the only source of the
Provider-visible Tool list.

Task correlation and recovery observers are intentionally left as injected
ports.  This initial composition does not create a second persistence or
recovery implementation.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from manyselves.application.runtime_services import RuntimeServicesView
from manyselves.capabilities.distribution_reporting.runtime.chief_runtime import (
    ChiefChapterAgentInvoker,
)
from manyselves.capabilities.distribution_reporting.runtime.models.agentic import (
    CHIEF_SECTION_RESULT_PART_IDS,
    TaskEnvelope,
)
from manyselves.capabilities.distribution_reporting.runtime.models.chief_chapter import (
    DeclarativeChiefChapterContext,
)
from manyselves.capabilities.distribution_reporting.runtime.models.inputs import (
    ChiefChapterLaneInput,
)
from manyselves.capabilities.distribution_reporting.runtime.module_provider import (
    ModuleProviderDependencies,
    build_module_provider_tools,
)
from manyselves.capabilities.distribution_reporting.runtime.storage import ReportingStore
from manyselves.core.artifacts.gateway import ArtifactGateway, ArtifactGrant
from manyselves.core.loops.agent_loop import AgentLoop
from manyselves.core.tools.result_memory import RunToolResultIndex
from manyselves.kernel.definitions import (
    AgentDefinition,
    RecoveryPolicyDefinition,
    TaskDefinition,
)
from manyselves.kernel.ports import AgentInvocationOutcome, AgentInvoker
from manyselves.runtime.agent_execution import AgentExecutionService, AgentSessionLoop
from manyselves.runtime.provider_agent_session import ProviderAgentSessionFactory

from .artifact_access import compile_agent_access, scoped_gateway

LoopBuilder = Callable[..., AgentSessionLoop]


def _chief_result_part_ids(contract: ChiefChapterLaneInput) -> list[str]:
    """Return the exact result-part scope for one prepared Chief lane."""

    if contract.chapter_id == "4":
        return ["special_topic_analysis"]
    return [CHIEF_SECTION_RESULT_PART_IDS[section_id] for section_id in contract.section_ids]


class ChiefProviderRuntime:
    """Build a real Provider-backed ``chief-editor`` invoker per task.

    ``ChiefChapterAgentInvoker`` retains the Capability prompt and typed
    decoder.  This owner creates only the configured Provider loop and the
    declared collaboration Tools for the prepared envelope.
    """

    workflow_id = "public-reporting"

    def __init__(
        self,
        services: RuntimeServicesView,
        *,
        store: ReportingStore | None = None,
        execution: AgentExecutionService | None = None,
        loop_builder: LoopBuilder = AgentLoop,
        dependencies: ModuleProviderDependencies | None = None,
    ) -> None:
        if services.workspace is None:
            raise ValueError("Chief Provider composition requires a workspace")
        self.services = services
        self.workspace = Path(services.workspace).resolve()
        self.store = store or ReportingStore(self.workspace)
        self.execution = execution or AgentExecutionService(services.bus)
        self.loop_builder = loop_builder
        self.dependencies = dependencies or ModuleProviderDependencies()
        self._artifact_root = ArtifactGateway(
            self.workspace,
            ArtifactGrant("root", "root", "workflow", "root"),
        )
        self.agent_invokers: Mapping[str, AgentInvoker] = {"chief-editor": self}

    async def close(self) -> None:
        await self.execution.close_workflow(self.workflow_id)

    async def invoke(
        self,
        agent: AgentDefinition,
        task: TaskDefinition,
        value: Any,
        conversation: Any,
        *,
        task_id: str,
    ) -> AgentInvocationOutcome:
        bridge = self._bridge(agent, task, value, conversation, task_id=task_id)
        return await bridge.invoke(
            agent,
            task,
            value,
            conversation,
            task_id=task_id,
        )

    async def invoke_with_recovery(
        self,
        agent: AgentDefinition,
        task: TaskDefinition,
        value: Any,
        conversation: Any,
        *,
        task_id: str,
        recovery_policy: RecoveryPolicyDefinition,
    ) -> AgentInvocationOutcome:
        bridge = self._bridge(agent, task, value, conversation, task_id=task_id)
        return await bridge.invoke_with_recovery(
            agent,
            task,
            value,
            conversation,
            task_id=task_id,
            recovery_policy=recovery_policy,
        )

    def _bridge(
        self,
        agent: AgentDefinition,
        task: TaskDefinition,
        value: Any,
        conversation: Any,
        *,
        task_id: str,
    ) -> ChiefChapterAgentInvoker:
        context = (
            value
            if isinstance(value, DeclarativeChiefChapterContext)
            else DeclarativeChiefChapterContext.model_validate(value)
        )
        contract = context.contract
        envelope = context.envelope
        if contract is None or envelope is None:
            raise ValueError("Chief Provider turn requires a prepared TaskEnvelope")

        session_id = conversation.external_session_id or (
            f"{self.workflow_id}:{conversation.key.value}"
        )
        runtime_id = f"{self.workflow_id}:{agent.id}:{conversation.key.value}"
        dependencies = self._compose_artifact_dependencies(
            agent,
            envelope,
            session_id=session_id,
        )

        # The TaskDefinition is authoritative: build_module_provider_tools may
        # know additional Capability tools, but it registers only this exact
        # task.tools sequence below.
        tools = build_module_provider_tools(
            self.workspace,
            envelope=envelope,
            module_id="chief",
            session_id=session_id,
            workflow_id=self.workflow_id,
            bus=self.services.bus,
            store=self.store,
            global_knowledge_root=self.services.global_knowledge_root,
            tool_names=list(task.tools),
            expected_part_ids=_chief_result_part_ids(contract),
            dependencies=dependencies,
        )
        loop_kwargs: dict[str, Any] = {
            "agent_type": runtime_id,
            "workspace": self.workspace,
            "tools": tools,
            "bus": self.services.bus,
            "config": self.services.agent_defaults,
            "llm_provider": self.services.active_provider,
            "system_prompt": agent.instructions,
            "usage_run_id": envelope.run_id,
            "usage_task_id": task_id,
            "artifact_gateway": dependencies.artifact_gateway,
        }
        session_factory = ProviderAgentSessionFactory(
            loop_builder=self.loop_builder,
            loop_kwargs=loop_kwargs,
            persist_handoff_summary=False,
        )
        existing = self.execution.session(self.workflow_id, conversation.key.value)
        if existing is not None:
            session_factory.reconfigure(existing.loop)
        return ChiefChapterAgentInvoker(
            self.workspace,
            execution=self.execution,
            session_factory=lambda _runtime_id: session_factory(),
            workflow_id=self.workflow_id,
            terminal_task_attempt_id=(
                dependencies.task_correlation.task_attempt_id
                if dependencies.task_correlation is not None
                else ""
            ),
        )

    def _compose_artifact_dependencies(
        self,
        agent: AgentDefinition,
        envelope: TaskEnvelope,
        *,
        session_id: str,
    ) -> ModuleProviderDependencies:
        """Project existing artifact access and result-index resources per task."""

        if (
            self.dependencies.artifact_gateway is not None
            or self.dependencies.artifact_access is not None
            or self.dependencies.result_index is not None
        ):
            return self.dependencies
        gateway = scoped_gateway(
            self._artifact_root,
            workflow_id=self.workflow_id,
            envelope=envelope,
            agent_id=agent.id,
            session_id=session_id,
        )
        access = compile_agent_access(agent, envelope, gateway=gateway)
        result_index = RunToolResultIndex(self.workspace, envelope.run_id)
        return ModuleProviderDependencies(
            artifact_gateway=gateway,
            artifact_access=access,
            result_index=result_index,
            task_correlation=self.dependencies.task_correlation,
            recovery_event_callback=self.dependencies.recovery_event_callback,
            tool_implementations=self.dependencies.tool_implementations,
            web_backend=self.dependencies.web_backend,
            research_guard=self.dependencies.research_guard,
        )


@dataclass(frozen=True, slots=True)
class ChiefProviderComposition:
    """Internal port combining Chief lifecycle and Provider invoker."""

    provider: ChiefProviderRuntime
    lifecycle: Any | None

    @property
    def chief_runtime(self) -> Any | None:
        """Expose the lifecycle port for composition callers."""

        return self.lifecycle

    @property
    def agent_invokers(self) -> Mapping[str, AgentInvoker]:
        return self.provider.agent_invokers

    @property
    def execution(self) -> AgentExecutionService:
        return self.provider.execution

    @property
    def workflow_id(self) -> str:
        return self.provider.workflow_id

    async def close(self) -> None:
        await self.provider.close()

    def _lifecycle_method(self, name: str, *args: Any, **kwargs: Any) -> Any:
        if self.lifecycle is None:
            raise AttributeError(f"Chief lifecycle port has no {name}")
        return getattr(self.lifecycle, name)(*args, **kwargs)

    def prepare(self, *args: Any, **kwargs: Any) -> Any:
        return self._lifecycle_method("prepare", *args, **kwargs)

    def prepare_lane(self, *args: Any, **kwargs: Any) -> Any:
        return self._lifecycle_method("prepare_lane", *args, **kwargs)

    def requires_agent(self, *args: Any, **kwargs: Any) -> Any:
        return self._lifecycle_method("requires_agent", *args, **kwargs)

    def accept_lane(self, *args: Any, **kwargs: Any) -> Any:
        return self._lifecycle_method("accept_lane", *args, **kwargs)

    def complete_lane(self, *args: Any, **kwargs: Any) -> Any:
        return self._lifecycle_method("complete_lane", *args, **kwargs)

    def reduce(self, *args: Any, **kwargs: Any) -> Any:
        return self._lifecycle_method("reduce", *args, **kwargs)


def build_chief_provider_composition(
    services: RuntimeServicesView,
    *,
    chief_runtime: Any | None = None,
    execution: AgentExecutionService | None = None,
    loop_builder: LoopBuilder = AgentLoop,
    store: ReportingStore | None = None,
    dependencies: ModuleProviderDependencies | None = None,
) -> ChiefProviderComposition:
    """Build the Chief lifecycle port with a real Provider invoker."""

    provider = ChiefProviderRuntime(
        services,
        store=store,
        execution=execution,
        loop_builder=loop_builder,
        dependencies=dependencies,
    )
    return ChiefProviderComposition(provider=provider, lifecycle=chief_runtime)


__all__ = [
    "ChiefProviderComposition",
    "ChiefProviderRuntime",
    "build_chief_provider_composition",
]

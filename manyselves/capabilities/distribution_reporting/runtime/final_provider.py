"""Provider composition for the file-defined Final Auditor lane.

The Final chapter bridge owns the chapter prompt and typed result decoding.
This module supplies the account-scoped Provider loop and the Tool set declared
by the task.  Artifact access, result indexing, task correlation, and recovery
remain injected or mechanically reused from their existing owners.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from manyselves.application.runtime_services import RuntimeServicesView
from manyselves.capabilities.distribution_reporting.runtime.final_agent_bridge import (
    FinalChapterAgentBridge,
)
from manyselves.capabilities.distribution_reporting.runtime.models.agentic import (
    TaskEnvelope,
)
from manyselves.capabilities.distribution_reporting.runtime.models.final_chapter import (
    DeclarativeFinalChapterContext,
)
from manyselves.capabilities.distribution_reporting.runtime.models.final_review import (
    DeclarativeFinalRecheckContext,
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


class FinalProviderRuntime:
    """Build one Provider-backed ``chief-editor-auditor`` invoker per lane."""

    workflow_id = "distribution-aggregate-existing-tail"

    def __init__(
        self,
        services: RuntimeServicesView,
        *,
        store: ReportingStore | None = None,
        execution: AgentExecutionService | None = None,
        loop_builder: LoopBuilder = AgentLoop,
        dependencies: ModuleProviderDependencies | None = None,
        workflow_id: str = "distribution-aggregate-existing-tail",
    ) -> None:
        if services.workspace is None:
            raise ValueError("Final Provider composition requires a workspace")
        self.services = services
        self.workspace = Path(services.workspace).resolve()
        self.store = store or ReportingStore(self.workspace)
        self.execution = execution or AgentExecutionService(services.bus)
        self.loop_builder = loop_builder
        self.dependencies = dependencies or ModuleProviderDependencies()
        self.workflow_id = workflow_id
        self._artifact_root = ArtifactGateway(
            self.workspace,
            ArtifactGrant("root", "root", "workflow", "root"),
        )
        self.agent_invokers: Mapping[str, AgentInvoker] = {
            "chief-editor-auditor": self,
        }

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
        bridge = self._bridge(agent, task, value, conversation)
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
        bridge = self._bridge(agent, task, value, conversation)
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
    ) -> FinalChapterAgentBridge:
        context = self._context(value)
        envelope = cast(TaskEnvelope, context.envelope)

        session_id = conversation.external_session_id or conversation.key.value
        runtime_id = f"{self.workflow_id}:{agent.id}:{conversation.key.value}"
        dependencies = self._compose_artifact_dependencies(
            agent,
            envelope,
            session_id=session_id,
        )
        tools = build_module_provider_tools(
            self.workspace,
            envelope=envelope,
            module_id=f"final-{context.chapter_id}",
            session_id=session_id,
            workflow_id=self.workflow_id,
            bus=self.services.bus,
            store=self.store,
            global_knowledge_root=self.services.global_knowledge_root,
            tool_names=list(task.tools),
            expected_part_ids=[],
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
            "usage_task_id": envelope.task_id,
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
        return FinalChapterAgentBridge(
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
        """Project existing scoped artifact access and result-index resources."""

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

    @staticmethod
    def _context(value: Any) -> DeclarativeFinalChapterContext | DeclarativeFinalRecheckContext:
        if isinstance(value, (DeclarativeFinalChapterContext, DeclarativeFinalRecheckContext)):
            return value
        raw_contract = value.get("contract") if isinstance(value, Mapping) else None
        phase = raw_contract.get("phase") if isinstance(raw_contract, Mapping) else None
        if phase == "recheck":
            return DeclarativeFinalRecheckContext.model_validate(value)
        return DeclarativeFinalChapterContext.model_validate(value)


@dataclass(frozen=True, slots=True)
class FinalProviderComposition:
    """Provider invoker composition for the declared Final chapter tasks."""

    provider: FinalProviderRuntime

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


def build_final_provider_composition(
    services: RuntimeServicesView,
    *,
    execution: AgentExecutionService | None = None,
    loop_builder: LoopBuilder = AgentLoop,
    store: ReportingStore | None = None,
    dependencies: ModuleProviderDependencies | None = None,
    workflow_id: str = "distribution-aggregate-existing-tail",
) -> FinalProviderComposition:
    """Build the initial Final Auditor Provider composition."""

    provider = FinalProviderRuntime(
        services,
        store=store,
        execution=execution,
        loop_builder=loop_builder,
        dependencies=dependencies,
        workflow_id=workflow_id,
    )
    return FinalProviderComposition(provider=provider)


__all__ = [
    "FinalProviderComposition",
    "FinalProviderRuntime",
    "build_final_provider_composition",
]

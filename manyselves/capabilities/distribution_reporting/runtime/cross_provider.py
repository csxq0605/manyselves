"""Provider composition for the file-defined initial Cross owner task.

The Cross owner lifecycle and typed prompt/result boundary remain owned by
``cross_owner_runtime``.  This module supplies the account-scoped Provider
resources and the declared collaboration Tool set for one initial owner lane.
It reuses the existing Module tool assembler so ``SubmitResultTool`` keeps its
existing TaskEnvelope and AgentResult wire contract.

Task correlation and recovery observers are injected ports.  This initial
composition does not create a second persistence or recovery implementation.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, cast

from manyselves.capabilities.distribution_reporting.runtime.agent_recovery_turn import (
    build_tool_recovery_callback,
)
from manyselves.capabilities.distribution_reporting.runtime.completed_result_recovery import (
    ProviderTaskAttempt,
    load_completed_agent_result,
    reporting_identity_key,
)
from manyselves.capabilities.distribution_reporting.runtime.continuation_progress import (
    ReportingContinuationProgressObserver,
)
from manyselves.capabilities.distribution_reporting.runtime.cross_owner_runtime import (
    CrossOwnerAgentInvoker,
)
from manyselves.capabilities.distribution_reporting.runtime.models.agentic import (
    TaskEnvelope,
)
from manyselves.capabilities.distribution_reporting.runtime.models.cross_owner import (
    DeclarativeCrossOwnerRuntimeContext,
)
from manyselves.capabilities.distribution_reporting.runtime.module_provider import (
    ModuleProviderDependencies,
    build_module_provider_tools,
)
from manyselves.capabilities.distribution_reporting.runtime.storage import ReportingStore
from manyselves.kernel.definitions import (
    AgentDefinition,
    RecoveryPolicyDefinition,
    TaskDefinition,
)
from manyselves.kernel.ports import AgentInvocationOutcome, AgentInvoker
from manyselves.runtime.agent_execution import AgentExecutionService, AgentSessionLoop
from manyselves.runtime.agent_recovery import AgentRecoveryDriver
from manyselves.runtime.artifacts.gateway import ArtifactGateway, ArtifactGrant
from manyselves.runtime.loops.agent_loop import AgentLoop
from manyselves.runtime.provider_agent_session import ProviderAgentSessionFactory
from manyselves.runtime.services import RuntimeServicesView
from manyselves.runtime.tools.result_memory import RunToolResultIndex

from .artifact_access import compile_agent_access, scoped_gateway
from .state.parallel import TaskCorrelation

LoopBuilder = Callable[..., AgentSessionLoop]


class CrossProviderRuntime:
    """Build a Provider-backed ``cross-module-reviewer`` invoker per lane."""

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
            raise ValueError("Cross Provider composition requires a workspace")
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
        self.agent_invokers: Mapping[str, AgentInvoker] = {
            "cross-module-reviewer": self,
            "main-agent": self,
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
        return await self._invoke(
            agent,
            task,
            value,
            conversation,
            task_id=task_id,
            recovery_policy=None,
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
        return await self._invoke(
            agent,
            task,
            value,
            conversation,
            task_id=task_id,
            recovery_policy=recovery_policy,
        )

    async def _invoke(
        self,
        agent: AgentDefinition,
        task: TaskDefinition,
        value: Any,
        conversation: Any,
        *,
        task_id: str,
        recovery_policy: RecoveryPolicyDefinition | None,
    ) -> AgentInvocationOutcome:
        context = (
            value
            if isinstance(value, DeclarativeCrossOwnerRuntimeContext)
            else DeclarativeCrossOwnerRuntimeContext.model_validate(value)
        )
        preparation = CrossOwnerAgentInvoker.preparation_for_output(
            context,
            task.output_contract,
        )
        if preparation is None or preparation.envelope is None:
            raise ValueError("Cross Provider turn requires a prepared TaskEnvelope")
        session_id = conversation.external_session_id or (
            f"{self.workflow_id}:{conversation.key.value}"
        )
        task_attempt: ProviderTaskAttempt | None = None
        if not isinstance(self.dependencies.task_correlation, TaskCorrelation):
            task_attempt = ProviderTaskAttempt.acquire(
                self.workspace,
                cast(TaskEnvelope, preparation.envelope),
                workflow_id=self.workflow_id,
                identity_key=reporting_identity_key(
                    agent.id,
                    conversation.key.value,
                ),
                session_id=session_id,
            )
        try:
            bridge = self._bridge(
                agent,
                task,
                context,
                conversation,
                recovery_policy=recovery_policy,
                task_attempt=task_attempt,
            )
            if recovery_policy is None:
                return await bridge.invoke(
                    agent,
                    task,
                    context,
                    conversation,
                    task_id=task_id,
                )
            return await bridge.invoke_with_recovery(
                agent,
                task,
                context,
                conversation,
                task_id=task_id,
                recovery_policy=recovery_policy,
            )
        finally:
            if task_attempt is not None:
                task_attempt.close()


    def _bridge(
        self,
        agent: AgentDefinition,
        task: TaskDefinition,
        value: Any,
        conversation: Any,
        *,
        recovery_policy: RecoveryPolicyDefinition | None = None,
        task_attempt: ProviderTaskAttempt | None = None,
    ) -> CrossOwnerAgentInvoker:
        context = (
            value
            if isinstance(value, DeclarativeCrossOwnerRuntimeContext)
            else DeclarativeCrossOwnerRuntimeContext.model_validate(value)
        )
        preparation = CrossOwnerAgentInvoker.preparation_for_output(
            context,
            task.output_contract,
        )
        envelope = cast(TaskEnvelope, preparation.envelope)

        session_id = conversation.external_session_id or (
            f"{self.workflow_id}:{conversation.key.value}"
        )
        runtime_id = f"{self.workflow_id}:{agent.id}:{conversation.key.value}"
        base_dependencies = (
            replace(
                self.dependencies,
                task_correlation=task_attempt.correlation,
            )
            if task_attempt is not None
            else self.dependencies
        )
        dependencies = self._compose_artifact_dependencies(
            agent,
            envelope,
            session_id=session_id,
            base_dependencies=base_dependencies,
        )
        recovery_driver = (
            AgentRecoveryDriver(recovery_policy)
            if recovery_policy is not None
            else None
        )
        if (
            recovery_driver is not None
            and dependencies.recovery_event_callback is None
        ):
            dependencies = replace(
                dependencies,
                recovery_event_callback=build_tool_recovery_callback(
                    recovery_driver
                ),
            )
        tools = build_module_provider_tools(
            self.workspace,
            envelope=envelope,
            module_id=context.owner_module_id,
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
        completed_result_loader = None
        if task_attempt is not None:
            completed_result_loader = task_attempt.load_completed_or_activate
        elif isinstance(dependencies.task_correlation, TaskCorrelation):
            def load_completed() -> Any:
                return load_completed_agent_result(
                    self.workspace,
                    dependencies.task_correlation,
                )

            completed_result_loader = load_completed
        return CrossOwnerAgentInvoker(
            self.workspace,
            execution=self.execution,
            session_factory=lambda _runtime_id: session_factory(),
            workflow_id=self.workflow_id,
            completed_result_loader=completed_result_loader,
            terminal_task_attempt_id=(
                dependencies.task_correlation.task_attempt_id
                if dependencies.task_correlation is not None
                else ""
            ),
            recovery_driver=recovery_driver,
            progress_observer=ReportingContinuationProgressObserver(
                self.workspace,
                envelope,
            ).observe,
        )

    def _compose_artifact_dependencies(
        self,
        agent: AgentDefinition,
        envelope: TaskEnvelope,
        *,
        session_id: str,
        base_dependencies: ModuleProviderDependencies | None = None,
    ) -> ModuleProviderDependencies:
        """Project existing scoped artifact access and result-index resources."""

        base = base_dependencies or self.dependencies
        if (
            base.artifact_gateway is not None
            or base.artifact_access is not None
            or base.result_index is not None
        ):
            return base
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
            task_correlation=base.task_correlation,
            recovery_event_callback=base.recovery_event_callback,
            tool_implementations=base.tool_implementations,
            web_backend=base.web_backend,
            research_guard=base.research_guard,
        )


@dataclass(frozen=True, slots=True)
class CrossProviderComposition:
    """Provider invoker plus an optional existing Cross lifecycle port."""

    provider: CrossProviderRuntime
    lifecycle: Any | None

    @property
    def cross_runtime(self) -> Any | None:
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


def build_cross_provider_composition(
    services: RuntimeServicesView,
    *,
    cross_runtime: Any | None = None,
    execution: AgentExecutionService | None = None,
    loop_builder: LoopBuilder = AgentLoop,
    store: ReportingStore | None = None,
    dependencies: ModuleProviderDependencies | None = None,
) -> CrossProviderComposition:
    """Build the initial Cross Provider composition."""

    provider = CrossProviderRuntime(
        services,
        store=store,
        execution=execution,
        loop_builder=loop_builder,
        dependencies=dependencies,
    )
    return CrossProviderComposition(provider=provider, lifecycle=cross_runtime)


__all__ = [
    "CrossProviderComposition",
    "CrossProviderRuntime",
    "build_cross_provider_composition",
]

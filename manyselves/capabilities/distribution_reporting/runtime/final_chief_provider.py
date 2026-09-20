"""Provider composition for the file-defined Final Chief revision lane.

The Final Chief bridge owns the revision prompt and typed result decoding.  This
module supplies the account-scoped Provider loop and declared collaboration
Tools declared by ``final-chief-chapter-revision``.  Artifact access, result
indexing, task correlation, and recovery remain existing injected resources.
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
from manyselves.capabilities.distribution_reporting.runtime.final_chief_agent_bridge import (
    FinalChiefAgentBridge,
)
from manyselves.capabilities.distribution_reporting.runtime.models.agentic import (
    TaskEnvelope,
)
from manyselves.capabilities.distribution_reporting.runtime.models.final_review import (
    DeclarativeFinalChiefRevisionContext,
)
from manyselves.capabilities.distribution_reporting.runtime.models.inputs import (
    ChiefChapterLaneInput,
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
from .models.reporting import CHIEF_SECTION_RESULT_PART_IDS
from .state.parallel import TaskCorrelation

LoopBuilder = Callable[..., AgentSessionLoop]


def _revision_result_part_ids(contract: ChiefChapterLaneInput) -> list[str]:
    """Resolve only the durable prose parts targeted by this revision lane."""

    section_ids = {
        section_id
        for finding in contract.assigned_findings
        for section_id in finding.target_section_ids
    }
    if contract.chapter_id == "4":
        return ["special_topic_analysis"]
    return [
        CHIEF_SECTION_RESULT_PART_IDS[section_id]
        for section_id in contract.section_ids
        if section_id in section_ids
    ]


class FinalChiefProviderRuntime:
    """Build one Provider-backed ``chief-editor`` revision invoker per lane."""

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
            raise ValueError("Final Chief Provider composition requires a workspace")
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
        context = DeclarativeFinalChiefRevisionContext.model_validate(value)
        envelope = cast(TaskEnvelope, context.envelope)
        task_attempt: ProviderTaskAttempt | None = None
        if not isinstance(self.dependencies.task_correlation, TaskCorrelation):
            session_id = conversation.external_session_id or conversation.key.value
            task_attempt = ProviderTaskAttempt.acquire(
                self.workspace,
                envelope,
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
    ) -> FinalChiefAgentBridge:
        context = DeclarativeFinalChiefRevisionContext.model_validate(value)
        contract = ChiefChapterLaneInput.model_validate(context.contract)
        envelope = cast(TaskEnvelope, context.envelope)
        session_id = conversation.external_session_id or conversation.key.value
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
            module_id="chief",
            session_id=session_id,
            workflow_id=self.workflow_id,
            bus=self.services.bus,
            store=self.store,
            global_knowledge_root=self.services.global_knowledge_root,
            tool_names=list(task.tools),
            expected_part_ids=_revision_result_part_ids(contract),
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
            persist_handoff_summary=True,
        )
        existing = self.execution.session(self.workflow_id, conversation.key.value)
        if existing is not None:
            session_factory.reconfigure(existing.loop)
        return FinalChiefAgentBridge(
            self.workspace,
            execution=self.execution,
            session_factory=lambda _runtime_id: session_factory(),
            workflow_id=self.workflow_id,
            completed_result_loader=self._completed_result_loader(
                task_attempt=task_attempt,
                expected=dependencies.task_correlation,
            ),
            terminal_task_attempt_id=(
                dependencies.task_correlation.task_attempt_id
                if isinstance(dependencies.task_correlation, TaskCorrelation)
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
        """Project existing scoped artifact and completed-result resources per task."""

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
        return replace(
            base,
            artifact_gateway=gateway,
            artifact_access=access,
            result_index=result_index,
        )

    def _completed_result_loader(
        self,
        *,
        task_attempt: ProviderTaskAttempt | None,
        expected: Any,
    ) -> Callable[[], Any | None] | None:
        if task_attempt is not None:
            return task_attempt.load_completed_or_activate
        if not isinstance(expected, TaskCorrelation):
            return None
        return lambda: load_completed_agent_result(self.workspace, expected)


@dataclass(frozen=True, slots=True)
class FinalChiefProviderComposition:
    """Provider invoker composition for the declared Final Chief task."""

    provider: FinalChiefProviderRuntime

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


def build_final_chief_provider_composition(
    services: RuntimeServicesView,
    *,
    execution: AgentExecutionService | None = None,
    loop_builder: LoopBuilder = AgentLoop,
    store: ReportingStore | None = None,
    dependencies: ModuleProviderDependencies | None = None,
    workflow_id: str = "distribution-aggregate-existing-tail",
) -> FinalChiefProviderComposition:
    """Build one Capability-owned Final Chief revision Provider composition."""

    return FinalChiefProviderComposition(
        provider=FinalChiefProviderRuntime(
            services,
            store=store,
            execution=execution,
            loop_builder=loop_builder,
            dependencies=dependencies,
            workflow_id=workflow_id,
        )
    )


__all__ = [
    "FinalChiefProviderComposition",
    "FinalChiefProviderRuntime",
    "build_final_chief_provider_composition",
]

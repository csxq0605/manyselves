"""Provider composition for the file-defined aggregate editor task.

This module supplies the account-scoped Provider resources for one aggregate
editor turn.  The aggregate prompt and typed result decoder remain in
``aggregate_agent_bridge``; this composition only projects the existing
TaskEnvelope/correlation identity, Capability tools, and generic Agent
session lifecycle.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from manyselves.application.runtime_services import RuntimeServicesView
from manyselves.capabilities.distribution_reporting.runtime.aggregate_agent_bridge import (
    AggregateEditorAgentBridge,
)
from manyselves.capabilities.distribution_reporting.runtime.models.agentic import (
    TaskEnvelope,
)
from manyselves.capabilities.distribution_reporting.runtime.models.inputs import (
    AggregateEditorInput,
)
from manyselves.capabilities.distribution_reporting.runtime.module_provider import (
    ModuleProviderDependencies,
    build_module_provider_tools,
)
from manyselves.capabilities.distribution_reporting.runtime.storage import ReportingStore
from manyselves.core.artifacts.gateway import ArtifactGateway, ArtifactGrant
from manyselves.core.loops.agent_loop import AgentLoop
from manyselves.core.tools.result_memory import RunToolResultIndex
from manyselves.kernel.conversations import ConversationRecord
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


class AggregateProviderRuntime:
    """Build one Provider-backed ``aggregate-editor`` invoker per task."""

    workflow_id = "distribution-aggregate-existing"

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
            raise ValueError("Aggregate Provider composition requires a workspace")
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
            "aggregate-editor": self,
        }

    async def close(self) -> None:
        await self.execution.close_workflow(self.workflow_id)

    async def invoke(
        self,
        agent: AgentDefinition,
        task: TaskDefinition,
        value: Any,
        conversation: ConversationRecord,
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
        conversation: ConversationRecord,
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
        conversation: ConversationRecord,
    ) -> AggregateEditorAgentBridge:
        contract = (
            value
            if isinstance(value, AggregateEditorInput)
            else AggregateEditorInput.model_validate(value)
        )
        correlation = self.dependencies.task_correlation
        workflow_id = str(
            getattr(correlation, "workflow_id", None) or self.workflow_id
        )
        envelope = self._envelope(agent, task, contract, correlation)
        session_id = conversation.external_session_id or conversation.key.value
        dependencies = self._compose_artifact_dependencies(
            agent,
            envelope,
            session_id=session_id,
        )
        tools = build_module_provider_tools(
            self.workspace,
            envelope=envelope,
            module_id="aggregate",
            session_id=session_id,
            workflow_id=workflow_id,
            bus=self.services.bus,
            store=self.store,
            global_knowledge_root=self.services.global_knowledge_root,
            tool_names=list(task.tools),
            expected_part_ids=[],
            dependencies=dependencies,
        )
        runtime_id = f"{workflow_id}:{agent.id}:{conversation.key.value}"
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
        existing = self.execution.session(workflow_id, conversation.key.value)
        if existing is not None:
            session_factory.reconfigure(existing.loop)
        return AggregateEditorAgentBridge(
            self.workspace,
            execution=self.execution,
            session_factory=lambda _runtime_id: session_factory(),
            workflow_id=workflow_id,
            terminal_sender=envelope.agent_id,
            terminal_task_id=envelope.task_id,
            terminal_task_attempt_id=(
                str(getattr(correlation, "task_attempt_id", ""))
                if correlation is not None
                else ""
            ),
        )

    @staticmethod
    def _envelope(
        agent: AgentDefinition,
        task: TaskDefinition,
        value: AggregateEditorInput,
        correlation: Any | None,
    ) -> TaskEnvelope:
        input_ref = (
            f"Work/runs/{value.run_id}/context/aggregate-editor-input.json"
        )
        envelope_values: dict[str, Any] = {
            "task_id": str(getattr(correlation, "task_id", None) or task.id),
            "run_id": value.run_id,
            "agent_id": str(getattr(correlation, "agent_id", None) or agent.id),
            "objective": task.objective,
            "input_refs": [input_ref],
            "allowed_outputs": [task.output_contract],
            "allowed_tools": list(task.tools),
            "input_contract_kind": task.input_contract,
            "input_contract_ref": input_ref,
            "inline_context": None,
        }
        if correlation is not None:
            envelope_values["task_attempt_id"] = str(correlation.task_attempt_id)
        return TaskEnvelope(
            **envelope_values,
        )

    def _compose_artifact_dependencies(
        self,
        agent: AgentDefinition,
        envelope: TaskEnvelope,
        *,
        session_id: str,
    ) -> ModuleProviderDependencies:
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
        return replace(
            self.dependencies,
            artifact_gateway=gateway,
            artifact_access=access,
            result_index=result_index,
        )


@dataclass(frozen=True, slots=True)
class AggregateProviderComposition:
    """Provider invoker composition for the aggregate editor task."""

    provider: AggregateProviderRuntime

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


def build_aggregate_provider_composition(
    services: RuntimeServicesView,
    *,
    execution: AgentExecutionService | None = None,
    loop_builder: LoopBuilder = AgentLoop,
    store: ReportingStore | None = None,
    dependencies: ModuleProviderDependencies | None = None,
) -> AggregateProviderComposition:
    """Build the initial aggregate Provider composition."""

    provider = AggregateProviderRuntime(
        services,
        store=store,
        execution=execution,
        loop_builder=loop_builder,
        dependencies=dependencies,
    )
    return AggregateProviderComposition(provider=provider)


__all__ = [
    "AggregateProviderComposition",
    "AggregateProviderRuntime",
    "build_aggregate_provider_composition",
]

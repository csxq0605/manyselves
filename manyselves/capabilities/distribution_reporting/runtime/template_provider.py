"""Internal Provider composition for the file-defined Template task.

This boundary consumes account-scoped Runtime resources and keeps Template
prompt/tool decoding in the Capability.  It deliberately owns only a typed
initial Agent turn; durable attempt/recovery observers and a run-level generic
continuation reader remain injectable Runtime concerns.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from manyselves.application.runtime_services import RuntimeServicesView
from manyselves.capabilities.distribution_reporting.runtime.agent_bridge import (
    TemplateDistillationAgentBridge,
)
from manyselves.capabilities.distribution_reporting.runtime.models.agentic import (
    TaskEnvelope,
)
from manyselves.capabilities.distribution_reporting.runtime.models.inputs import (
    TemplateDistillationInput,
)
from manyselves.capabilities.distribution_reporting.runtime.template_distillation import (
    TEMPLATE_DISTILLATION_CONSTRAINTS,
    TEMPLATE_DISTILLATION_INPUT_REF,
)
from manyselves.capabilities.distribution_reporting.runtime.template_tools import (
    build_template_distillation_provider_tools,
)
from manyselves.core.loops.agent_loop import AgentLoop
from manyselves.kernel.conversations import ConversationRecord
from manyselves.kernel.definitions import (
    AgentDefinition,
    RecoveryPolicyDefinition,
    TaskDefinition,
)
from manyselves.kernel.ports import AgentInvocationOutcome
from manyselves.runtime.agent_execution import (
    AgentExecutionService,
    AgentSessionLoop,
)
from manyselves.runtime.provider_agent_session import ProviderAgentSessionFactory

LoopBuilder = Callable[..., AgentSessionLoop]


class TemplateDistillationProviderRuntime:
    """Build one Capability-owned Template Agent invoker from account resources."""

    workflow_id = "distill-template-skill"

    def __init__(
        self,
        services: RuntimeServicesView,
        *,
        execution: AgentExecutionService | None = None,
        loop_builder: LoopBuilder = AgentLoop,
    ) -> None:
        self.services = services
        self.execution = execution or AgentExecutionService(services.bus)
        self.loop_builder = loop_builder

    async def close(self) -> None:
        """Close this workflow's shared generic Agent sessions."""

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
        input_value = self._input(value)
        bridge = self._bridge(agent, task, input_value, conversation, task_id=task_id)
        return await bridge.invoke(
            agent,
            task,
            input_value,
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
        input_value = self._input(value)
        bridge = self._bridge(agent, task, input_value, conversation, task_id=task_id)
        return await bridge.invoke_with_recovery(
            agent,
            task,
            input_value,
            conversation,
            task_id=task_id,
            recovery_policy=recovery_policy,
        )

    def _bridge(
        self,
        agent: AgentDefinition,
        task: TaskDefinition,
        value: TemplateDistillationInput,
        conversation: ConversationRecord,
        *,
        task_id: str,
    ) -> TemplateDistillationAgentBridge:
        workspace = self.services.workspace
        input_ref = TEMPLATE_DISTILLATION_INPUT_REF.format(run_id=value.run_id)
        envelope = TaskEnvelope(
            # The Host action id is also the typed terminal/tool persistence
            # identity for this dispatch.  Keeping one value here preserves
            # the existing bridge correlation without inventing an attempt
            # or durable recovery policy.
            task_id=task_id,
            run_id=value.run_id,
            agent_id=agent.id,
            objective=task.objective,
            input_refs=[input_ref, value.template_ref],
            constraints=list(TEMPLATE_DISTILLATION_CONSTRAINTS),
            allowed_outputs=[task.output_contract],
            allowed_tools=list(task.tools),
            input_contract_kind=task.input_contract,
            input_contract_ref=input_ref,
        )
        session_id = conversation.external_session_id or (
            f"{self.workflow_id}:{conversation.key.value}"
        )
        runtime_id = f"{self.workflow_id}:{agent.id}:{conversation.key.value}"
        tools = build_template_distillation_provider_tools(
            workspace,
            envelope=envelope,
            template_input=value,
            session_id=session_id,
            workflow_id=self.workflow_id,
            bus=self.services.bus,
            store=self._store(workspace),
            tool_names=task.tools,
        )
        loop_kwargs = {
            "agent_type": runtime_id,
            "workspace": workspace,
            "tools": tools,
            "bus": self.services.bus,
            "config": self.services.agent_defaults,
            "llm_provider": self.services.active_provider,
            "system_prompt": agent.instructions,
            "usage_run_id": value.run_id,
            "usage_task_id": task.id,
        }
        session_factory = ProviderAgentSessionFactory(
            loop_builder=self.loop_builder,
            loop_kwargs=loop_kwargs,
            persist_handoff_summary=False,
        )
        existing = self.execution.session(self.workflow_id, conversation.key.value)
        if existing is not None:
            session_factory.reconfigure(existing.loop)
        return TemplateDistillationAgentBridge(
            workspace,
            execution=self.execution,
            session_factory=lambda _runtime_id: session_factory(),
            workflow_id=self.workflow_id,
        )

    @staticmethod
    def _input(value: Any) -> TemplateDistillationInput:
        if isinstance(value, TemplateDistillationInput):
            return value
        return TemplateDistillationInput.model_validate(value)

    @staticmethod
    def _store(workspace: Path):
        from manyselves.capabilities.distribution_reporting.runtime.storage import (
            ReportingStore,
        )

        return ReportingStore(workspace)


__all__ = ["TemplateDistillationProviderRuntime"]

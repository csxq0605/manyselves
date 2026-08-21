"""Adapter from neutral Agent invocation to the current Reporting runner."""

from collections.abc import Callable, Mapping
from typing import Any

from pydantic import BaseModel

from manyselves.core.reporting.config import AgentDefinition as ReportingAgentDefinition
from manyselves.kernel.conversations import ConversationRecord
from manyselves.kernel.definitions import (
    AgentDefinition,
    RecoveryPolicyDefinition,
    TaskDefinition,
)
from manyselves.kernel.ports import AgentInvocationOutcome


class LegacyReportingAgentAdapter:
    """Bind generic definitions while preserving the runner's session-key behavior."""

    def __init__(
        self,
        runner: Any,
        definitions: Mapping[str, ReportingAgentDefinition],
        *,
        envelope_factory: Callable[
            [AgentDefinition, TaskDefinition, Any, ConversationRecord, str], Any
        ],
        workflow_id: str,
        shared_artifacts: Callable[[Any], list[str]] | None = None,
    ) -> None:
        self._runner = runner
        self._definitions = definitions
        self._envelope_factory = envelope_factory
        self._workflow_id = workflow_id
        self._shared_artifacts = shared_artifacts or (lambda _value: [])

    async def invoke(
        self,
        agent: AgentDefinition,
        task: TaskDefinition,
        value: Any,
        conversation: ConversationRecord,
        *,
        task_id: str,
    ) -> AgentInvocationOutcome:
        return await self._invoke(
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
        """Forward a declared policy through the private legacy bridge."""

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
        conversation: ConversationRecord,
        *,
        task_id: str,
        recovery_policy: RecoveryPolicyDefinition | None = None,
    ) -> AgentInvocationOutcome:
        try:
            definition = self._definitions[agent.id]
        except KeyError:
            return AgentInvocationOutcome(
                status="failed",
                error=f"missing legacy reporting definition: {agent.id}",
            )
        envelope = self._envelope_factory(
            agent,
            task,
            value,
            conversation,
            task_id,
        )
        run_kwargs: dict[str, Any] = {
            "workflow_id": self._workflow_id,
            "session_key": conversation.key.value,
        }
        if recovery_policy is not None:
            run_kwargs["recovery_policy"] = recovery_policy
        result = await self._runner.run(
            definition,
            envelope,
            self._shared_artifacts(value),
            **run_kwargs,
        )
        status = str(result.status)
        if status == "completed":
            outcome_status = "ok"
        elif status in {"blocked", "incomplete", "failed"}:
            outcome_status = status
        else:
            outcome_status = "failed"
        payload = result.payload
        if isinstance(payload, BaseModel):
            payload = payload.model_dump(mode="json")
        conversation.external_session_id = result.session_id
        return AgentInvocationOutcome(
            status=outcome_status,
            result=payload,
            session_id=result.session_id,
            error=getattr(result, "reason", None),
        )

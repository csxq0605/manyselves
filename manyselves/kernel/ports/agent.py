"""Business-neutral agent invocation port and outcome."""

from typing import Any, Literal, Protocol

from pydantic import BaseModel

from manyselves.kernel.conversations import ConversationRecord
from manyselves.kernel.definitions import (
    AgentDefinition,
    RecoveryPolicyDefinition,
    TaskDefinition,
)


class AgentInvocationOutcome(BaseModel):
    status: Literal["ok", "blocked", "incomplete", "failed"] = "ok"
    result: Any = None
    session_id: str | None = None
    error: str | None = None


class AgentInvoker(Protocol):
    async def invoke(
        self,
        agent: AgentDefinition,
        task: TaskDefinition,
        value: Any,
        conversation: ConversationRecord,
        *,
        task_id: str,
    ) -> AgentInvocationOutcome: ...


class RecoveryAwareAgentInvoker(AgentInvoker, Protocol):
    """Optional port for invokers that consume declared recovery policies."""

    async def invoke_with_recovery(
        self,
        agent: AgentDefinition,
        task: TaskDefinition,
        value: Any,
        conversation: ConversationRecord,
        *,
        task_id: str,
        recovery_policy: RecoveryPolicyDefinition,
    ) -> AgentInvocationOutcome: ...

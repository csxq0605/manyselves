"""Stable semantic events for comparing legacy and declarative executions.

The trace deliberately carries logical identities only. It does not persist a
runtime log, validate workflow behavior, or add scheduling decisions.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum


class SemanticEventKind(StrEnum):
    WORKFLOW_STARTED = "workflow.started"
    ACTION_STARTED = "action.started"
    CONVERSATION_CREATED = "conversation.created"
    AGENT_INVOKED = "agent.invoked"
    TOOL_INVOKED = "tool.invoked"
    CONTRACT_VALIDATED = "contract.validated"
    BRANCH_SELECTED = "branch.selected"
    ACTION_COMPLETED = "action.completed"
    ACTION_FAILED = "action.failed"
    WORKFLOW_COMPLETED = "workflow.completed"
    OUTPUT_PUBLISHED = "output.published"


@dataclass(frozen=True, slots=True)
class SemanticTraceEvent:
    """One comparison event without timestamps, random IDs, paths, or model text."""

    event: SemanticEventKind
    workflow_id: str | None = None
    action_id: str | None = None
    agent_id: str | None = None
    conversation_key: str | None = None
    input_contract: str | None = None
    output_contract: str | None = None
    tool_id: str | None = None
    branch: str | None = None
    status: str | None = None
    output_id: str | None = None

    def as_record(self) -> dict[str, str]:
        """Return a deterministic JSON-ready record with absent fields omitted."""

        return {
            key: str(value)
            for key, value in asdict(self).items()
            if value is not None
        }


class SemanticTraceRecorder:
    """Collect semantic events in observed execution order."""

    def __init__(self) -> None:
        self.events: list[SemanticTraceEvent] = []

    def record(
        self,
        event: SemanticEventKind,
        *,
        workflow_id: str | None = None,
        action_id: str | None = None,
        agent_id: str | None = None,
        conversation_key: str | None = None,
        input_contract: str | None = None,
        output_contract: str | None = None,
        tool_id: str | None = None,
        branch: str | None = None,
        status: str | None = None,
        output_id: str | None = None,
    ) -> None:
        self.events.append(
            SemanticTraceEvent(
                event=event,
                workflow_id=workflow_id,
                action_id=action_id,
                agent_id=agent_id,
                conversation_key=conversation_key,
                input_contract=input_contract,
                output_contract=output_contract,
                tool_id=tool_id,
                branch=branch,
                status=status,
                output_id=output_id,
            )
        )

    def snapshot(self) -> list[dict[str, str]]:
        return [event.as_record() for event in self.events]

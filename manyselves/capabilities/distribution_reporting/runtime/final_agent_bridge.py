"""Capability-owned bridge for one typed initial Final Auditor turn.

The bridge adapts the declared Final lane contract to the neutral
``AgentExecutionService``.  It deliberately stops after decoding one typed
``FinalChapterLaneFindingSubmission``; recovery, acceptance, reduction, Final
review rounds, and Delivery belong to later Capability slices.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from manyselves.capabilities.distribution_reporting.runtime.models.agentic import (
    FinalChapterLaneFindingSubmission,
)
from manyselves.capabilities.distribution_reporting.runtime.models.final_chapter import (
    DeclarativeFinalChapterAgentResult,
    DeclarativeFinalChapterContext,
)
from manyselves.capabilities.distribution_reporting.runtime.models.inputs import (
    FinalChapterLaneInput,
)
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
    AgentTurnRequest,
)
from manyselves.runtime.typed_agent_turn import TypedAgentTurn

SessionFactory = Callable[[str], AgentSessionLoop]


class FinalChapterAgentBridge:
    """Invoke one typed Final Auditor turn through the neutral Agent service."""

    def __init__(
        self,
        workspace: Path,
        *,
        execution: AgentExecutionService,
        session_factory: SessionFactory,
        workflow_id: str = "distribution-aggregate-existing-tail",
    ) -> None:
        self.workspace = Path(workspace).resolve()
        self.execution = execution
        self.session_factory = session_factory
        self.workflow_id = workflow_id

    async def invoke(
        self,
        agent: AgentDefinition,
        task: TaskDefinition,
        value: Any,
        conversation: ConversationRecord,
        *,
        task_id: str,
    ) -> AgentInvocationOutcome:
        return await self._invoke_once(
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
        """Keep the declared recovery port while this slice remains initial-only."""

        del recovery_policy
        return await self._invoke_once(
            agent,
            task,
            value,
            conversation,
            task_id=task_id,
        )

    async def _invoke_once(
        self,
        agent: AgentDefinition,
        task: TaskDefinition,
        value: Any,
        conversation: ConversationRecord,
        *,
        task_id: str,
    ) -> AgentInvocationOutcome:
        context = DeclarativeFinalChapterContext.model_validate(value)
        contract = FinalChapterLaneInput.model_validate(context.contract)
        runtime_id = self._runtime_id(agent, conversation)
        session_id = conversation.external_session_id or conversation.key.value
        typed_turn = TypedAgentTurn(
            execution=self.execution,
            workflow_id=self.workflow_id,
            conversation_key=conversation.key.value,
            runtime_id=runtime_id,
            session_id=session_id,
            session_factory=lambda: self.session_factory(runtime_id),
        )
        try:
            session = await typed_turn.start_or_restore()
        except Exception as exc:
            return AgentInvocationOutcome(
                status="failed",
                session_id=conversation.external_session_id,
                error=str(exc),
            )

        conversation.external_session_id = session.session_id
        begin_typed_task = getattr(session.loop, "begin_typed_task", None)
        if callable(begin_typed_task):
            begin_typed_task(
                {
                    "task_id": task.id,
                    "run_id": contract.run_id,
                    "input_contract": task.input_contract,
                    "output_contract": task.output_contract,
                }
            )

        request = AgentTurnRequest(
            content=self._prompt(
                agent,
                task,
                contract,
                inline_context=(
                    context.envelope.inline_context
                    if context.envelope is not None
                    else None
                ),
            ),
            message_id=(
                f"{task_id}:{contract.run_id}:chapter-{context.chapter_id}:initial"
            ),
            workflow_id=self.workflow_id,
            run_id=contract.run_id,
            task_id=task_id,
            task_attempt_id=task_id,
            turn_kind="task_initial",
        )
        terminal = typed_turn.result_terminal(
            run_id=contract.run_id,
            task_id=task_id,
            task_attempt_id=task_id,
            session_id=session.session_id,
        )
        outcome = await typed_turn.dispatch(
            session,
            request,
            terminals=(terminal,),
        )
        return TypedAgentTurn.map_outcome(
            outcome,
            session_id=session.session_id,
            decode_result=self._decode_result,
        )

    def _prompt(
        self,
        agent: AgentDefinition,
        task: TaskDefinition,
        value: FinalChapterLaneInput,
        *,
        inline_context: str | None,
    ) -> str:
        sections = [
            agent.instructions,
            f"Task: {task.objective}",
            json.dumps(
                value.model_dump(mode="json"),
                ensure_ascii=False,
                indent=2,
            ),
            f"Allowed tools: {json.dumps(task.tools, ensure_ascii=False)}",
            f"Output contract: {task.output_contract}",
        ]
        if inline_context:
            sections.append(f"Inline context:\n{inline_context}")
        return "\n\n".join(
            sections
        )

    def _read_submission(self, result_ref: str) -> FinalChapterLaneFindingSubmission:
        path = Path(result_ref)
        path = path if path.is_absolute() else self.workspace / path
        return FinalChapterLaneFindingSubmission.model_validate(
            json.loads(path.read_text(encoding="utf-8"))
        )

    def _decode_result(self, result_ref: str) -> dict[str, Any]:
        submission = self._read_submission(result_ref)
        return DeclarativeFinalChapterAgentResult(
            status="completed",
            submission=submission,
        ).model_dump(mode="json")

    def _runtime_id(
        self,
        agent: AgentDefinition,
        conversation: ConversationRecord,
    ) -> str:
        return f"{self.workflow_id}:{agent.id}:{conversation.key.value}"


__all__ = ["FinalChapterAgentBridge"]

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
from manyselves.interfaces.types import AgentResponse, AgentResultMessage
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
    AgentTerminalSubscription,
    AgentTurnRequest,
)

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
        try:
            session = await self.execution.start_or_restore(
                workflow_id=self.workflow_id,
                conversation_key=conversation.key.value,
                runtime_id=runtime_id,
                session_id=session_id,
                session_factory=lambda: self.session_factory(runtime_id),
            )
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
            content=self._prompt(agent, task, contract),
            message_id=(
                f"{task_id}:{contract.run_id}:chapter-{context.chapter_id}:initial"
            ),
            workflow_id=self.workflow_id,
            run_id=contract.run_id,
            task_id=task_id,
            task_attempt_id=task_id,
            turn_kind="task_initial",
        )
        terminal = AgentTerminalSubscription(
            kind="typed_result",
            message_type=AgentResultMessage,
            predicate=lambda item: self._matches_result(
                item,
                runtime_id=runtime_id,
                run_id=contract.run_id,
                task_id=task_id,
                session_id=session.session_id,
            ),
        )
        outcome = await self.execution.dispatch_turn(
            session,
            request,
            terminals=(terminal,),
        )
        if outcome.kind == "typed_result":
            message = outcome.message
            if not isinstance(message, AgentResultMessage):
                return AgentInvocationOutcome(
                    status="failed",
                    session_id=session.session_id,
                    error="typed terminal was not an AgentResultMessage",
                )
            if message.status != "completed":
                status = "blocked" if message.status == "blocked" else "incomplete"
                return AgentInvocationOutcome(
                    status=status,
                    session_id=session.session_id,
                    error=message.status,
                )
            try:
                submission = self._read_submission(message.result_path)
            except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
                return AgentInvocationOutcome(
                    status="failed",
                    session_id=session.session_id,
                    error=str(exc),
                )
            result = DeclarativeFinalChapterAgentResult(
                status="completed",
                submission=submission,
            )
            return AgentInvocationOutcome(
                status="ok",
                result=result.model_dump(mode="json"),
                session_id=session.session_id,
            )
        if outcome.kind == "error":
            return AgentInvocationOutcome(
                status="failed",
                session_id=session.session_id,
                error=str(outcome.message),
            )
        if isinstance(outcome.message, AgentResponse):
            return AgentInvocationOutcome(
                status="incomplete",
                session_id=session.session_id,
                error="Agent turn ended without a typed result",
            )
        return AgentInvocationOutcome(
            status="failed",
            session_id=session.session_id,
            error="Agent turn returned an unknown terminal",
        )

    def _prompt(
        self,
        agent: AgentDefinition,
        task: TaskDefinition,
        value: FinalChapterLaneInput,
    ) -> str:
        return "\n\n".join(
            (
                agent.instructions,
                f"Task: {task.objective}",
                json.dumps(
                    value.model_dump(mode="json"),
                    ensure_ascii=False,
                    indent=2,
                ),
                f"Allowed tools: {json.dumps(task.tools, ensure_ascii=False)}",
                f"Output contract: {task.output_contract}",
            )
        )

    def _read_submission(self, result_ref: str) -> FinalChapterLaneFindingSubmission:
        path = Path(result_ref)
        path = path if path.is_absolute() else self.workspace / path
        return FinalChapterLaneFindingSubmission.model_validate(
            json.loads(path.read_text(encoding="utf-8"))
        )

    def _runtime_id(
        self,
        agent: AgentDefinition,
        conversation: ConversationRecord,
    ) -> str:
        return f"{self.workflow_id}:{agent.id}:{conversation.key.value}"

    def _matches_result(
        self,
        item: AgentResultMessage,
        *,
        runtime_id: str,
        run_id: str,
        task_id: str,
        session_id: str,
    ) -> bool:
        return (
            item.sender == runtime_id
            and item.workflow_id == self.workflow_id
            and item.run_id == run_id
            and item.task_id == task_id
            and item.task_attempt_id == task_id
            and item.session_id == session_id
        )


__all__ = ["FinalChapterAgentBridge"]

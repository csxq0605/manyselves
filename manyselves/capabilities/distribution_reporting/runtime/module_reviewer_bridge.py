"""Capability-owned bridge for one typed initial module Reviewer turn.

The bridge adapts the prepared module review envelope to the neutral Agent
execution port and decodes one typed finding submission.  Review acceptance,
revision, recheck, and recovery remain separate Capability actions.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

from manyselves.capabilities.distribution_reporting.runtime.agent_result_payload import (
    load_agent_result_payload,
)
from manyselves.capabilities.distribution_reporting.runtime.models.agentic import (
    ModuleReviewFindingSubmission,
    ModuleReviewVerdictSubmission,
    TaskEnvelope,
)
from manyselves.capabilities.distribution_reporting.runtime.models.inputs import (
    ModuleReviewInput,
)
from manyselves.capabilities.distribution_reporting.runtime.models.module_lane import (
    DeclarativeModuleRecheckAgentResult,
    DeclarativeModuleReviewAgentResult,
    DeclarativeModuleRuntimeLaneContext,
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


class ModuleReviewerAgentBridge:
    """Invoke one typed initial module review through the neutral Agent service."""

    def __init__(
        self,
        workspace: Path,
        *,
        execution: AgentExecutionService,
        session_factory: SessionFactory,
        workflow_id: str = "public-reporting",
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
        """Keep the declared port while this slice remains initial-only."""

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
        context = DeclarativeModuleRuntimeLaneContext.model_validate(value)
        envelope, review_input, inline_context = self._prepared_turn(context)
        workflow_id = context.workflow_id or self.workflow_id
        run_id = envelope.run_id
        runtime_id = self._runtime_id(agent, conversation, workflow_id)
        session_id = conversation.external_session_id or (
            f"{workflow_id}:{conversation.key.value}"
        )
        typed_turn = TypedAgentTurn(
            execution=self.execution,
            workflow_id=workflow_id,
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
                    "run_id": run_id,
                    "input_contract": task.input_contract,
                    "output_contract": task.output_contract,
                }
            )

        request = AgentTurnRequest(
            content=self._prompt(
                agent,
                task,
                review_input,
                inline_context=inline_context,
            ),
            message_id=f"{task_id}:{run_id}:initial",
            workflow_id=workflow_id,
            run_id=run_id,
            task_id=task_id,
            task_attempt_id=task_id,
            turn_kind="task_initial",
        )
        terminal = typed_turn.result_terminal(
            run_id=run_id,
            task_id=task_id,
            task_attempt_id=task_id,
            session_id=session.session_id,
            sender=envelope.agent_id,
            terminal_task_id=envelope.task_id,
            terminal_task_attempt_id="",
        )
        outcome = await typed_turn.dispatch(
            session,
            request,
            terminals=(terminal,),
        )
        return TypedAgentTurn.map_outcome(
            outcome,
            session_id=session.session_id,
            decode_result=lambda result_ref: self._decode_result(
                result_ref,
                output_contract=task.output_contract,
            ),
        )

    @staticmethod
    def _prompt(
        agent: AgentDefinition,
        task: TaskDefinition,
        value: ModuleReviewInput,
        *,
        inline_context: str | None,
    ) -> str:
        sections = [
            agent.instructions,
            f"Task: {task.objective}",
            json.dumps(value.model_dump(mode="json"), ensure_ascii=False, indent=2),
            f"Allowed tools: {json.dumps(task.tools, ensure_ascii=False)}",
            f"Output contract: {task.output_contract}",
        ]
        if inline_context:
            sections.append(f"Inline context:\n{inline_context}")
        return "\n\n".join(sections)

    def _decode_result(
        self,
        result_ref: str,
        *,
        output_contract: str,
    ) -> dict[str, Any]:
        loaded = load_agent_result_payload(self.workspace, result_ref)
        if output_contract == "declarative_module_recheck_agent_result":
            submission = ModuleReviewVerdictSubmission.model_validate(
                loaded.payload
            )
            return DeclarativeModuleRecheckAgentResult(
                status="completed",
                submission=submission,
            ).model_dump(mode="json")
        submission = ModuleReviewFindingSubmission.model_validate(
            loaded.payload
        )
        return DeclarativeModuleReviewAgentResult(
            status="completed",
            submission=submission,
        ).model_dump(mode="json")

    @staticmethod
    def _prepared_turn(
        context: DeclarativeModuleRuntimeLaneContext,
    ) -> tuple[TaskEnvelope, ModuleReviewInput, str | None]:
        if context.recheck is not None:
            prepared = context.recheck.prepared
        else:
            prepared = context.review.prepared
        envelope = cast(TaskEnvelope, prepared.envelope)
        review_input = cast(ModuleReviewInput, prepared.review_input)
        return (
            cast(TaskEnvelope, envelope),
            cast(ModuleReviewInput, review_input),
            envelope.inline_context,
        )

    @staticmethod
    def _runtime_id(
        agent: AgentDefinition,
        conversation: ConversationRecord,
        workflow_id: str,
    ) -> str:
        return f"{workflow_id}:{agent.id}:{conversation.key.value}"


__all__ = ["ModuleReviewerAgentBridge"]

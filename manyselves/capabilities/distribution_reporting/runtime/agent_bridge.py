"""Capability-owned first Agent bridge for template Skill distillation.

This slice deliberately stops at the smallest production boundary that can be
proved here: a typed input is rendered into one generic Agent turn, the
``AgentExecutionService`` owns a stable conversation session, and a typed
``AgentResultMessage`` is decoded back into the Capability output contract.

Completed-result reuse now uses the existing neutral recovery service when a
Capability-owned loader supplies an already verified typed result.  Correction,
continuation, and no-progress remain outside this bridge until their existing
durable reporting implementations can be reused without copying their policy
or persistence semantics.
"""

from __future__ import annotations

import inspect
import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from manyselves.capabilities.distribution_reporting.runtime.models.agentic import (
    TemplateSkillSubmission,
)
from manyselves.capabilities.distribution_reporting.runtime.models.inputs import (
    TemplateDistillationInput,
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
from manyselves.runtime.agent_recovery import AgentRecoveryDriver

SessionFactory = Callable[[str], AgentSessionLoop]
CompletedResultLoader = Callable[
    [AgentDefinition, TaskDefinition, TemplateDistillationInput, ConversationRecord],
    Any | Awaitable[Any] | None,
]


class TemplateDistillationAgentBridge:
    """Adapt the typed template task to the neutral Agent execution port.

    The factory is injected so the first slice can be characterized with a
    scripted ``AgentSessionLoop``.  It is intentionally not a wrapper around
    ``ReportingAgentRunner`` or ``ReportWorkflowRunner``.
    """

    def __init__(
        self,
        workspace: Path,
        *,
        execution: AgentExecutionService,
        session_factory: SessionFactory,
        workflow_id: str = "distill-template-skill",
        completed_result_loader: CompletedResultLoader | None = None,
    ) -> None:
        self.workspace = Path(workspace).resolve()
        self.execution = execution
        self.session_factory = session_factory
        self.workflow_id = workflow_id
        self.completed_result_loader = completed_result_loader

    def _runtime_id(
        self,
        agent: AgentDefinition,
        conversation: ConversationRecord,
    ) -> str:
        return f"{self.workflow_id}:{agent.id}:{conversation.key.value}"

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
            recovery_policy=None,
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
        """Reuse a Capability-loaded completion, then run one typed turn.

        Only the existing neutral ``completed_tool_result`` decision is
        interpreted here.  Other recovery events still use the same initial
        turn until their durable Capability-owned implementations are reused.
        """

        return await self._invoke_once(
            agent,
            task,
            value,
            conversation,
            task_id=task_id,
            recovery_policy=recovery_policy,
        )

    async def _invoke_once(
        self,
        agent: AgentDefinition,
        task: TaskDefinition,
        value: Any,
        conversation: ConversationRecord,
        *,
        task_id: str,
        recovery_policy: RecoveryPolicyDefinition | None,
    ) -> AgentInvocationOutcome:
        input_value = (
            value
            if isinstance(value, TemplateDistillationInput)
            else TemplateDistillationInput.model_validate(value)
        )
        if self.completed_result_loader is not None:
            persisted = self.completed_result_loader(
                agent,
                task,
                input_value,
                conversation,
            )
            if inspect.isawaitable(persisted):
                persisted = await persisted
            if persisted is not None:
                submission = (
                    persisted
                    if isinstance(persisted, TemplateSkillSubmission)
                    else TemplateSkillSubmission.model_validate(persisted)
                )

                async def reuse_completed(_directive: Any) -> AgentInvocationOutcome:
                    return self._completed_outcome(
                        submission,
                        conversation.external_session_id,
                    )

                async def stop_completed(directive: Any) -> AgentInvocationOutcome:
                    return self._incomplete_outcome(
                        directive.reason or "completed result recovery stopped",
                        conversation.external_session_id,
                    )

                recovered = await self.execution.recover_completed_result(
                    recovery=AgentRecoveryDriver(recovery_policy),
                    detail={
                        "task_id": task.id,
                        "source": "persisted_result",
                    },
                    reuse_result=reuse_completed,
                    stop=stop_completed,
                )
                if isinstance(recovered, AgentInvocationOutcome):
                    return recovered
        runtime_id = self._runtime_id(agent, conversation)
        session_id = conversation.external_session_id or (
            f"{self.workflow_id}:{conversation.key.value}"
        )
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
                    "run_id": input_value.run_id,
                    "input_contract": task.input_contract,
                    "output_contract": task.output_contract,
                }
            )

        request = AgentTurnRequest(
            content=self._prompt(agent, task, input_value),
            message_id=f"{task_id}:{input_value.run_id}:initial",
            workflow_id=self.workflow_id,
            run_id=input_value.run_id,
            task_id=task_id,
            # The generic service requires a correlation value.  The Host
            # action id is the existing durable dispatch identity for this
            # first slice; no new attempt or replay policy is introduced here.
            task_attempt_id=task_id,
            turn_kind="task_initial",
        )
        terminal = AgentTerminalSubscription(
            kind="typed_result",
            message_type=AgentResultMessage,
            predicate=lambda item: self._matches_result(
                item,
                runtime_id=runtime_id,
                run_id=input_value.run_id,
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
                    status=status,  # type: ignore[arg-type]
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
            return AgentInvocationOutcome(
                status="ok",
                result=submission.model_dump(mode="json"),
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

    @staticmethod
    def _completed_outcome(
        submission: TemplateSkillSubmission,
        session_id: str | None,
    ) -> AgentInvocationOutcome:
        return AgentInvocationOutcome(
            status="ok",
            result=submission.model_dump(mode="json"),
            session_id=session_id,
        )

    @staticmethod
    def _incomplete_outcome(
        error: str,
        session_id: str | None,
    ) -> AgentInvocationOutcome:
        return AgentInvocationOutcome(
            status="incomplete",
            session_id=session_id,
            error=error,
        )

    def _prompt(
        self,
        agent: AgentDefinition,
        task: TaskDefinition,
        value: TemplateDistillationInput,
    ) -> str:
        return "\n\n".join(
            (
                agent.instructions,
                f"Task: {task.objective}",
                json.dumps(
                    {
                        "run_id": value.run_id,
                        "template_ref": value.template_ref,
                        "inspect_max_chars": value.inspect_max_chars,
                        "required_part_ids": value.required_part_ids,
                        "allowed_tools": task.tools,
                        "output_contract": task.output_contract,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
            )
        )

    def _read_submission(self, result_ref: str) -> TemplateSkillSubmission:
        path = Path(result_ref)
        path = path if path.is_absolute() else self.workspace / path
        return TemplateSkillSubmission.model_validate(
            json.loads(path.read_text(encoding="utf-8"))
        )

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


__all__ = ["TemplateDistillationAgentBridge"]

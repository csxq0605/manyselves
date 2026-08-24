"""Capability-owned bridge for one typed Final Chief revision turn."""

from __future__ import annotations

import inspect
import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from manyselves.capabilities.distribution_reporting.runtime.agent_recovery_turn import (
    ProgressObserver,
    execute_reporting_recovery,
)
from manyselves.capabilities.distribution_reporting.runtime.agent_result_payload import (
    load_agent_result_payload,
)
from manyselves.capabilities.distribution_reporting.runtime.models.agentic import (
    AgentResult,
    ChiefChapterLaneRevisionSubmission,
    TaskEnvelope,
)
from manyselves.capabilities.distribution_reporting.runtime.models.final_review import (
    DeclarativeFinalChiefRevisionAgentResult,
    DeclarativeFinalChiefRevisionContext,
)
from manyselves.capabilities.distribution_reporting.runtime.models.inputs import (
    ChiefChapterLaneInput,
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
from manyselves.runtime.agent_recovery import AgentRecoveryDriver
from manyselves.runtime.typed_agent_turn import TypedAgentTurn

SessionFactory = Callable[[str], AgentSessionLoop]
CompletedResultLoader = Callable[[], Any | Awaitable[Any] | None]


class FinalChiefAgentBridge:
    """Invoke one typed Chief revision turn through the neutral Agent service."""

    def __init__(
        self,
        workspace: Path,
        *,
        execution: AgentExecutionService,
        session_factory: SessionFactory,
        workflow_id: str = "distribution-aggregate-existing-tail",
        completed_result_loader: CompletedResultLoader | None = None,
        terminal_task_attempt_id: str = "",
        recovery_driver: AgentRecoveryDriver | None = None,
        progress_observer: ProgressObserver | None = None,
    ) -> None:
        self.workspace = Path(workspace).resolve()
        self.execution = execution
        self.session_factory = session_factory
        self.workflow_id = workflow_id
        self.completed_result_loader = completed_result_loader
        self.terminal_task_attempt_id = terminal_task_attempt_id
        self.recovery_driver = recovery_driver
        self.progress_observer = progress_observer

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
        recovery_policy: RecoveryPolicyDefinition | None = None,
    ) -> AgentInvocationOutcome:
        context = DeclarativeFinalChiefRevisionContext.model_validate(value)
        contract = ChiefChapterLaneInput.model_validate(context.contract)
        envelope = TaskEnvelope.model_validate(context.envelope)
        if self.completed_result_loader is not None:
            persisted = self.completed_result_loader()
            if inspect.isawaitable(persisted):
                persisted = await persisted
            if persisted is not None:
                if not isinstance(persisted, AgentResult):
                    persisted = AgentResult.model_validate(persisted)
                conversation.external_session_id = persisted.session_id

                async def reuse_completed(_directive: Any) -> AgentInvocationOutcome:
                    return AgentInvocationOutcome(
                        status="ok",
                        result=self._decode_payload(persisted.payload),
                        session_id=persisted.session_id,
                    )

                async def stop_completed(directive: Any) -> AgentInvocationOutcome:
                    return AgentInvocationOutcome(
                        status="incomplete",
                        session_id=persisted.session_id,
                        error=(
                            getattr(directive, "reason", None)
                            or "completed result recovery stopped"
                        ),
                    )

                recovered = await self.execution.recover_completed_result(
                    recovery=(
                        self.recovery_driver
                        or AgentRecoveryDriver(recovery_policy)
                    ),
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
                f"{task_id}:{contract.run_id}:chapter-"
                f"{context.chapter_id}:revision-{contract.revision}"
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
            sender=envelope.agent_id,
            terminal_task_id=envelope.task_id,
            terminal_task_attempt_id=self.terminal_task_attempt_id,
        )
        def decode_result(result_ref: str) -> dict[str, Any]:
            return DeclarativeFinalChiefRevisionAgentResult(
                status="completed",
                submission=self._read_submission(result_ref),
            ).model_dump(mode="json")
        if recovery_policy is None:
            outcome = await typed_turn.dispatch(
                session,
                request,
                terminals=(terminal,),
            )
            return typed_turn.map_outcome(
                outcome,
                session_id=session.session_id,
                decode_result=decode_result,
            )
        recovered = await execute_reporting_recovery(
            self.execution,
            session,
            request,
            recovery_policy=recovery_policy,
            terminals=(terminal,),
            prompt_builder=lambda event_kind: self._recovery_prompt(
                agent,
                task,
                contract,
                inline_context=envelope.inline_context,
                event_kind=event_kind,
            ),
            result_decoder=decode_result,
            recovery=self.recovery_driver,
            progress_observer=self.progress_observer,
        )
        if isinstance(recovered, AgentInvocationOutcome):
            return recovered
        return AgentInvocationOutcome(
            status="ok",
            result=recovered,
            session_id=session.session_id,
        )

    def _recovery_prompt(
        self,
        agent: AgentDefinition,
        task: TaskDefinition,
        contract: ChiefChapterLaneInput,
        *,
        inline_context: str | None,
        event_kind: Any,
    ) -> str:
        event_name = getattr(event_kind, "value", str(event_kind))
        return "\n\n".join(
            (
                f"<{event_name}>",
                "继续当前 Final Chief 会话，复用已完成的分析和工具结果；"
                "立即调用 submit_result 提交符合 output contract 的类型化修订。",
                self._prompt(
                    agent,
                    task,
                    contract,
                    inline_context=inline_context,
                ),
                f"</{event_name}>",
            )
        )

    @staticmethod
    def _prompt(
        agent: AgentDefinition,
        task: TaskDefinition,
        value: ChiefChapterLaneInput,
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
        return "\n\n".join(
            sections
        )

    def _read_submission(self, result_ref: str) -> ChiefChapterLaneRevisionSubmission:
        return ChiefChapterLaneRevisionSubmission.model_validate(
            load_agent_result_payload(self.workspace, result_ref).payload
        )

    @staticmethod
    def _decode_payload(payload: Any) -> dict[str, Any]:
        return DeclarativeFinalChiefRevisionAgentResult(
            status="completed",
            submission=ChiefChapterLaneRevisionSubmission.model_validate(payload),
        ).model_dump(mode="json")

    def _runtime_id(
        self,
        agent: AgentDefinition,
        conversation: ConversationRecord,
    ) -> str:
        return f"{self.workflow_id}:{agent.id}:{conversation.key.value}"

__all__ = ["FinalChiefAgentBridge"]

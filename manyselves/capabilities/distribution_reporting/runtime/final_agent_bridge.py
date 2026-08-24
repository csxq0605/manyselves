"""Capability-owned bridge for one typed Final Auditor turn.

The bridge adapts the declared Final lane contract to the neutral
``AgentExecutionService``.  It deliberately stops after decoding one typed
initial finding or recheck verdict submission; recovery, acceptance, reduction,
Final review rounds, and Delivery belong to later Capability slices.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, cast

from manyselves.capabilities.distribution_reporting.runtime.agent_recovery_turn import (
    execute_reporting_recovery,
)
from manyselves.capabilities.distribution_reporting.runtime.agent_result_payload import (
    load_agent_result_payload,
)
from manyselves.capabilities.distribution_reporting.runtime.models.agentic import (
    FinalChapterLaneFindingSubmission,
    FinalChapterLaneVerdictSubmission,
    TaskEnvelope,
)
from manyselves.capabilities.distribution_reporting.runtime.models.final_chapter import (
    DeclarativeFinalChapterAgentResult,
    DeclarativeFinalChapterContext,
)
from manyselves.capabilities.distribution_reporting.runtime.models.final_review import (
    DeclarativeFinalRecheckAgentResult,
    DeclarativeFinalRecheckContext,
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
from manyselves.runtime.agent_recovery import AgentRecoveryDriver
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
        terminal_task_attempt_id: str = "",
        recovery_driver: AgentRecoveryDriver | None = None,
    ) -> None:
        self.workspace = Path(workspace).resolve()
        self.execution = execution
        self.session_factory = session_factory
        self.workflow_id = workflow_id
        self.terminal_task_attempt_id = terminal_task_attempt_id
        self.recovery_driver = recovery_driver

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
        raw_contract = (
            value.get("contract")
            if isinstance(value, Mapping)
            else getattr(value, "contract", None)
        )
        phase = (
            raw_contract.get("phase")
            if isinstance(raw_contract, Mapping)
            else getattr(raw_contract, "phase", None)
        )
        if phase == "recheck":
            context = DeclarativeFinalRecheckContext.model_validate(value)
            return await self._invoke_typed(
                agent,
                task,
                FinalChapterLaneInput.model_validate(context.contract),
                conversation,
                task_id=task_id,
                envelope=cast(TaskEnvelope, context.envelope),
                inline_context=(
                    context.envelope.inline_context
                    if context.envelope is not None
                    else None
                ),
                decode_result=self._decode_recheck_result,
                turn_suffix="recheck",
                recovery_policy=recovery_policy,
            )
        context = DeclarativeFinalChapterContext.model_validate(value)
        return await self._invoke_typed(
            agent,
            task,
            FinalChapterLaneInput.model_validate(context.contract),
            conversation,
            task_id=task_id,
            envelope=cast(TaskEnvelope, context.envelope),
            inline_context=(
                context.envelope.inline_context
                if context.envelope is not None
                else None
            ),
            decode_result=self._decode_result,
            turn_suffix="initial",
            recovery_policy=recovery_policy,
        )

    async def _invoke_typed(
        self,
        agent: AgentDefinition,
        task: TaskDefinition,
        contract: FinalChapterLaneInput,
        conversation: ConversationRecord,
        *,
        task_id: str,
        envelope: TaskEnvelope,
        inline_context: str | None,
        decode_result: Callable[[str], dict[str, Any]],
        turn_suffix: str,
        recovery_policy: RecoveryPolicyDefinition | None,
    ) -> AgentInvocationOutcome:
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
                inline_context=inline_context,
            ),
            message_id=f"{task_id}:{contract.run_id}:chapter-{contract.chapter_id}:{turn_suffix}",
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
        if recovery_policy is None:
            outcome = await typed_turn.dispatch(
                session,
                request,
                terminals=(terminal,),
            )
            return TypedAgentTurn.map_outcome(
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
                inline_context=inline_context,
                event_kind=event_kind,
            ),
            result_decoder=decode_result,
            recovery=self.recovery_driver,
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
        contract: FinalChapterLaneInput,
        *,
        inline_context: str | None,
        event_kind: Any,
    ) -> str:
        event_name = getattr(event_kind, "value", str(event_kind))
        return "\n\n".join(
            (
                f"<{event_name}>",
                "继续当前 Final Auditor 会话，复用已完成的分析和工具结果；"
                "立即调用 submit_result 提交符合 output contract 的类型化结果。",
                self._prompt(
                    agent,
                    task,
                    contract,
                    inline_context=inline_context,
                ),
                f"</{event_name}>",
            )
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
        return FinalChapterLaneFindingSubmission.model_validate(
            load_agent_result_payload(self.workspace, result_ref).payload
        )

    def _decode_result(self, result_ref: str) -> dict[str, Any]:
        submission = self._read_submission(result_ref)
        return DeclarativeFinalChapterAgentResult(
            status="completed",
            submission=submission,
        ).model_dump(mode="json")

    def _read_verdict_submission(self, result_ref: str) -> FinalChapterLaneVerdictSubmission:
        return FinalChapterLaneVerdictSubmission.model_validate(
            load_agent_result_payload(self.workspace, result_ref).payload
        )

    def _decode_recheck_result(self, result_ref: str) -> dict[str, Any]:
        submission = self._read_verdict_submission(result_ref)
        return DeclarativeFinalRecheckAgentResult(
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

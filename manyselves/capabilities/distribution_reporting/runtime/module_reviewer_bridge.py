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

from manyselves.capabilities.distribution_reporting.runtime.agent_recovery_turn import (
    execute_reporting_recovery,
)
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
from manyselves.runtime.agent_recovery import AgentRecoveryDriver
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
        recovery_driver: AgentRecoveryDriver | None = None,
    ) -> None:
        self.workspace = Path(workspace).resolve()
        self.execution = execution
        self.session_factory = session_factory
        self.workflow_id = workflow_id
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
        """Run declared recovery on the same Reviewer session."""

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
        if recovery_policy is None:
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

        recovered = await execute_reporting_recovery(
            self.execution,
            session,
            request,
            recovery_policy=recovery_policy,
            terminals=(terminal,),
            prompt_builder=lambda event_kind: self._recovery_prompt(
                agent,
                task,
                context,
                event_kind,
            ),
            result_decoder=lambda result_ref: self._decode_result(
                result_ref,
                output_contract=task.output_contract,
            ),
            recovery=self.recovery_driver,
        )
        if isinstance(recovered, AgentInvocationOutcome):
            return recovered
        return AgentInvocationOutcome(
            status="ok",
            result=recovered,
            session_id=session.session_id,
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
    def _recovery_prompt(
        agent: AgentDefinition,
        task: TaskDefinition,
        context: DeclarativeModuleRuntimeLaneContext,
        event_kind: Any,
    ) -> str:
        event_name = getattr(event_kind, "value", str(event_kind))
        if event_name == "max_tokens":
            instruction = (
                "继续当前会话中的 module review 工作；上轮达到 max_tokens，"
                "不要重做已完成的工具或审阅。"
            )
        elif event_name == "tool_slice_boundary":
            instruction = (
                "继续当前会话中的 module review 工作；复用已有 tool slice 结果，"
                "不要重放已经完成的工具。"
            )
        else:
            instruction = (
                "上一轮没有提交结构化 review 结果；在当前会话中立即完成"
                " submission_correction，不要重复已完成的审阅。"
            )
        return "\n\n".join(
            (
                f"<{event_name}>",
                instruction,
                f"你仍是 {agent.id}，当前任务是 {task.id}。",
                "立即调用 submit_result，提交符合 output contract 的类型化结果。",
                json.dumps(
                    {
                        "module_context": context.model_dump(mode="json"),
                        "output_contract": task.output_contract,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                f"</{event_name}>",
            )
        )

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

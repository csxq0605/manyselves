"""Capability-owned first Agent bridge for template Skill distillation.

This slice deliberately stops at the smallest production boundary that can be
proved here: a typed input is rendered into one generic Agent turn, the
``AgentExecutionService`` owns a stable conversation session, and a typed
``AgentResultMessage`` is decoded back into the Capability output contract.

Completed-result reuse now uses the existing neutral recovery service when a
Capability-owned loader supplies an already verified typed result.  Natural
language without a submission now uses the same generic recovery driver for
one Capability-owned typed correction turn.  The existing AgentLoop max-token
marker and tool-slice marker also use that driver for one continuation turn.
Schema-invalid tool submissions and durable no-progress recovery remain outside
this bridge until their existing implementations can be observed without
copying their policy or persistence semantics.
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
from manyselves.kernel.recovery import RecoveryActionKind, RecoveryEventKind
from manyselves.runtime.agent_execution import (
    AgentExecutionService,
    AgentRecoveryCompleted,
    AgentRecoveryDirective,
    AgentRecoveryObservation,
    AgentRecoveryProgress,
    AgentRecoveryRequired,
    AgentRecoveryStopped,
    AgentSessionLoop,
    AgentTurnOutcome,
    AgentTurnRequest,
)
from manyselves.runtime.agent_recovery import AgentRecoveryDriver
from manyselves.runtime.loops.agent_loop import (
    AGENT_MAX_TOKENS_CONTINUATION_REQUIRED,
    AGENT_TURN_CONTINUATION_REQUIRED,
)
from manyselves.runtime.typed_agent_turn import TypedAgentTurn

from .agent_recovery_turn import ProgressObserver

SessionFactory = Callable[[str], AgentSessionLoop]
CompletedResultLoader = Callable[
    [AgentDefinition, TaskDefinition, TemplateDistillationInput, ConversationRecord],
    Any | Awaitable[Any] | None,
]


class TemplateDistillationAgentBridge:
    """Adapt the typed template task to the neutral Agent execution port.

    The injected session factory keeps Provider construction outside this
    typed prompt/result adapter.
    """

    def __init__(
        self,
        workspace: Path,
        *,
        execution: AgentExecutionService,
        session_factory: SessionFactory,
        workflow_id: str = "distill-template-skill",
        completed_result_loader: CompletedResultLoader | None = None,
        terminal_task_attempt_id: str | None = None,
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

        Existing completed-result, natural-language, and AgentLoop max-token
        and tool-slice boundaries are interpreted here.  Schema-invalid tool
        submissions are still owned by the Reporting tool callback because the
        neutral ``ToolResult`` message has no task/session correlation.
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
        session_id = conversation.external_session_id or (
            f"{self.workflow_id}:{conversation.key.value}"
        )
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
                    "run_id": input_value.run_id,
                    "input_contract": task.input_contract,
                    "output_contract": task.output_contract,
                }
            )

        initial_request = AgentTurnRequest(
            content=self._prompt(agent, task, input_value),
            message_id=f"{task_id}:{input_value.run_id}:initial",
            workflow_id=self.workflow_id,
            run_id=input_value.run_id,
            task_id=task_id,
            # The generic service requires a correlation value.  The Host
            # action id is the existing durable dispatch identity for this
            # first slice; no new attempt or replay policy is introduced here.
            task_attempt_id=self.terminal_task_attempt_id or task_id,
            turn_kind="task_initial",
        )
        terminal = typed_turn.result_terminal(
            run_id=input_value.run_id,
            task_id=task_id,
            task_attempt_id=self.terminal_task_attempt_id or task_id,
            session_id=session.session_id,
        )
        if recovery_policy is None:
            outcome = await typed_turn.dispatch(
                session,
                initial_request,
                terminals=(terminal,),
            )
            return typed_turn.map_outcome(
                outcome,
                session_id=session.session_id,
                decode_result=lambda result_ref: self._read_submission(
                    result_ref
                ).model_dump(mode="json"),
            )

        async def interpret(
            outcome: AgentTurnOutcome,
            _request: AgentTurnRequest,
        ) -> AgentRecoveryObservation:
            if outcome.kind == "typed_result":
                message = outcome.message
                if not isinstance(message, AgentResultMessage):
                    return AgentRecoveryStopped(
                        reason="typed terminal was not an AgentResultMessage"
                    )
                if message.status != "completed":
                    return AgentRecoveryStopped(reason=message.status)
                try:
                    return AgentRecoveryCompleted(
                        result=self._read_submission(message.result_path)
                    )
                except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
                    return AgentRecoveryStopped(reason=str(exc))
            if outcome.kind == "error":
                return AgentRecoveryStopped(reason=str(outcome.message))
            if isinstance(outcome.message, AgentResponse):
                if (
                    outcome.message.content
                    == AGENT_MAX_TOKENS_CONTINUATION_REQUIRED
                ):
                    required = AgentRecoveryRequired(
                        event_kind=RecoveryEventKind.MAX_TOKENS,
                        fallback_action=RecoveryActionKind.CONTINUE,
                        detail={"task_id": task.id},
                    )
                elif outcome.message.content == AGENT_TURN_CONTINUATION_REQUIRED:
                    required = AgentRecoveryRequired(
                        event_kind=RecoveryEventKind.TOOL_SLICE_BOUNDARY,
                        fallback_action=RecoveryActionKind.CONTINUE,
                        detail={"task_id": task.id},
                    )
                else:
                    return AgentRecoveryRequired(
                        event_kind=RecoveryEventKind.NATURAL_LANGUAGE_WITHOUT_SUBMISSION,
                        fallback_action=RecoveryActionKind.CORRECT,
                        detail={"task_id": task.id},
                    )
                if self.progress_observer is None:
                    return required
                progress_detail = dict(required.detail or {})
                return AgentRecoveryProgress(
                    kind=await self.progress_observer(
                        session.loop,
                        required.event_kind,
                        progress_detail,
                    ),
                    continuation=required,
                    detail=progress_detail,
                )
            return AgentRecoveryStopped(reason="Agent turn returned an unknown terminal")

        async def build_turn(
            directive: AgentRecoveryDirective,
            _observation: AgentRecoveryRequired,
        ) -> AgentTurnRequest:
            max_tokens_continuation = directive.event_kind is RecoveryEventKind.MAX_TOKENS
            tool_slice_continuation = (
                directive.event_kind is RecoveryEventKind.TOOL_SLICE_BOUNDARY
            )
            return AgentTurnRequest(
                content=(
                    directive.prompt
                    or (
                        self._max_tokens_prompt(agent, task)
                        if max_tokens_continuation
                        else self._tool_slice_prompt(agent, task)
                        if tool_slice_continuation
                        else self._correction_prompt(agent, task)
                    )
                ),
                message_id=(
                    f"{task_id}:{input_value.run_id}:max-tokens-continuation"
                    if max_tokens_continuation
                    else f"{task_id}:{input_value.run_id}:tool-slice-continuation"
                    if tool_slice_continuation
                    else f"{task_id}:{input_value.run_id}:correction"
                ),
                workflow_id=self.workflow_id,
                run_id=input_value.run_id,
                task_id=task_id,
                task_attempt_id=task_id,
                internal=True,
                turn_kind=(
                    "max_tokens_continuation"
                    if max_tokens_continuation
                    else "tool_slice_continuation"
                    if tool_slice_continuation
                    else "submission_correction"
                ),
            )

        async def stop(
            outcome: AgentTurnOutcome,
            directive: AgentRecoveryDirective,
        ) -> AgentInvocationOutcome:
            if isinstance(outcome.message, AgentResponse):
                return self._incomplete_outcome(
                    directive.reason or "Agent turn ended without a typed result",
                    session.session_id,
                )
            return self._incomplete_outcome(
                directive.reason or str(outcome.message),
                session.session_id,
            )

        async def reuse_result(
            outcome: AgentTurnOutcome,
            _directive: AgentRecoveryDirective,
        ) -> TemplateSkillSubmission:
            if not isinstance(outcome.message, AgentResultMessage):
                raise ValueError("reusable terminal was not an AgentResultMessage")
            return self._read_submission(outcome.message.result_path)

        recovered = await self.execution.execute_with_recovery(
            session,
            initial_request,
            recovery=(self.recovery_driver or AgentRecoveryDriver(recovery_policy)),
            interpret=interpret,
            build_turn=build_turn,
            stop=stop,
            reuse_result=reuse_result,
            terminals=(terminal,),
        )
        if isinstance(recovered, AgentInvocationOutcome):
            return recovered
        if isinstance(recovered, TemplateSkillSubmission):
            return self._completed_outcome(recovered, session.session_id)
        return AgentInvocationOutcome(
            status="failed",
            session_id=session.session_id,
            error="Agent recovery returned an unknown result",
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

    @staticmethod
    def _correction_prompt(
        agent: AgentDefinition,
        task: TaskDefinition,
    ) -> str:
        return "\n\n".join(
            (
                "<submission_correction>",
                "上一轮只返回了自然语言，尚未提交类型化结果。",
                f"你仍是 {agent.id}，当前任务是 {task.id}。",
                "不要重新读取模板或重复已经完成的分析；使用当前会话上下文，"
                "立即调用 submit_result 提交 template_skill_submission。",
                "参数必须直接符合 output contract，不要添加 payload 包装或 JSON 字符串。",
                "</submission_correction>",
            )
        )

    @staticmethod
    def _max_tokens_prompt(
        agent: AgentDefinition,
        task: TaskDefinition,
    ) -> str:
        return "\n\n".join(
            (
                "<max_tokens_continuation>",
                "上一轮 Agent 输出达到单次 max_tokens 上限，任务尚未完成。",
                f"你仍是 {agent.id}，当前任务是 {task.id}；继续使用同一会话上下文，"
                "不要从头重复读取或检索。完成后立即调用 submit_result 提交"
                " template_skill_submission。",
                "</max_tokens_continuation>",
            )
        )

    @staticmethod
    def _tool_slice_prompt(
        agent: AgentDefinition,
        task: TaskDefinition,
    ) -> str:
        return "\n\n".join(
            (
                "<tool_slice_continuation>",
                "上一轮达到工具执行片段边界，任务尚未完成；已完成的 Tool result 已保留。",
                f"你仍是 {agent.id}，当前任务是 {task.id}；使用同一会话和已有上下文，"
                "只继续尚未完成的部分，不要重跑已完成的工具或重读已有结果。"
                "完成后立即调用 submit_result 提交 template_skill_submission。",
                "</tool_slice_continuation>",
            )
        )

    def _read_submission(self, result_ref: str) -> TemplateSkillSubmission:
        path = Path(result_ref)
        path = path if path.is_absolute() else self.workspace / path
        return TemplateSkillSubmission.model_validate(
            json.loads(path.read_text(encoding="utf-8"))
        )

__all__ = ["TemplateDistillationAgentBridge"]

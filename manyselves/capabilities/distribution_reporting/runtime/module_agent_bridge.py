"""Capability-owned bridge for typed module Author Agent turns.

The bridge adapts the file-defined module Author task to the neutral
``AgentExecutionService``.  Recovery marker interpretation and prompt text
remain Capability-owned; session lifecycle and recovery progression stay in
the generic runtime.
"""

from __future__ import annotations

import inspect
import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, cast

from manyselves.capabilities.distribution_reporting.runtime.agent_recovery_turn import (
    execute_reporting_recovery,
)
from manyselves.capabilities.distribution_reporting.runtime.agent_result_payload import (
    load_agent_result_payload,
)
from manyselves.capabilities.distribution_reporting.runtime.models.agentic import (
    AgentResult,
    ModuleRevisionSubmission,
    ModuleSubmission,
    TaskEnvelope,
)
from manyselves.capabilities.distribution_reporting.runtime.models.module_lane import (
    DeclarativeModuleAuthoringAgentResult,
    DeclarativeModuleRevisionAgentResult,
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
CompletedResultLoader = Callable[
    [], AgentResult | None | Awaitable[AgentResult | None]
]


class ModuleAuthoringAgentBridge:
    """Invoke one module Author turn through the generic Agent service."""

    def __init__(
        self,
        workspace: Path,
        *,
        execution: AgentExecutionService,
        session_factory: SessionFactory,
        workflow_id: str = "public-reporting",
        completed_result_loader: CompletedResultLoader | None = None,
        terminal_task_attempt_id: str | None = None,
    ) -> None:
        self.workspace = Path(workspace).resolve()
        self.execution = execution
        self.session_factory = session_factory
        self.workflow_id = workflow_id
        self.completed_result_loader = completed_result_loader
        self.terminal_task_attempt_id = terminal_task_attempt_id

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
        context = DeclarativeModuleRuntimeLaneContext.model_validate(value)
        envelope = cast(TaskEnvelope, self._envelope(context))
        workflow_id = context.workflow_id or self.workflow_id
        run_id = envelope.run_id
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
                        result=self._decode_payload(
                            persisted.payload,
                            output_contract=task.output_contract,
                        ),
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
            content=self._prompt(agent, task, context),
            message_id=f"{task_id}:{run_id}:initial",
            workflow_id=workflow_id,
            run_id=run_id,
            task_id=task_id,
            task_attempt_id=self.terminal_task_attempt_id or task_id,
            turn_kind="task_initial",
        )
        terminal = typed_turn.result_terminal(
            run_id=run_id,
            task_id=task_id,
            task_attempt_id=task_id,
            session_id=session.session_id,
            sender=envelope.agent_id,
            terminal_task_id=envelope.task_id,
            terminal_task_attempt_id=self.terminal_task_attempt_id or "",
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
        )
        if isinstance(recovered, AgentInvocationOutcome):
            return recovered
        return AgentInvocationOutcome(
            status="ok",
            result=recovered,
            session_id=session.session_id,
        )

    def _prompt(
        self,
        agent: AgentDefinition,
        task: TaskDefinition,
        context: DeclarativeModuleRuntimeLaneContext,
    ) -> str:
        return "\n\n".join(
            (
                agent.instructions,
                f"Task: {task.objective}",
                json.dumps(
                    {
                        "module_context": context.model_dump(mode="json"),
                        "allowed_tools": task.tools,
                        "output_contract": task.output_contract,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
            )
        )

    def _decode_result(
        self,
        result_ref: str,
        *,
        output_contract: str,
    ) -> dict[str, Any]:
        loaded = load_agent_result_payload(self.workspace, result_ref)
        return self._decode_payload(
            loaded.payload,
            output_contract=output_contract,
        )

    @staticmethod
    def _decode_payload(
        payload: Any,
        *,
        output_contract: str,
    ) -> dict[str, Any]:
        if output_contract == "declarative_module_revision_agent_result":
            submission = ModuleRevisionSubmission.model_validate(
                payload
            )
            return DeclarativeModuleRevisionAgentResult(
                status="completed",
                submission=submission,
            ).model_dump(mode="json")
        submission = ModuleSubmission.model_validate(payload)
        return DeclarativeModuleAuthoringAgentResult(
            status="completed",
            module=submission,
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
                "继续当前会话中已开始的 module author 工作；上轮达到 max_tokens，"
                "不要重做已完成的工具或分析。"
            )
        elif event_name == "tool_slice_boundary":
            instruction = (
                "继续当前会话中的 module author 工作；复用已有 tool slice 结果，"
                "不要重放已经完成的工具。"
            )
        else:
            instruction = (
                "上一轮没有提交结构化结果；在当前会话中立即完成 submission_correction，"
                "不要重复已完成的分析。"
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
    def _envelope(
        context: DeclarativeModuleRuntimeLaneContext,
    ) -> TaskEnvelope | None:
        if context.authoring is not None:
            return context.authoring.envelope
        if context.revision is not None:
            return context.revision.prepared.envelope
        return None

    def _runtime_id(
        self,
        agent: AgentDefinition,
        conversation: ConversationRecord,
        workflow_id: str,
    ) -> str:
        return f"{workflow_id}:{agent.id}:{conversation.key.value}"


__all__ = ["ModuleAuthoringAgentBridge"]

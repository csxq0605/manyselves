"""Provider-backed ``photo-reader`` invoker: one photo, sent as an image, per task.

The workflow resets the lane conversation before each photo.  The lane keeps one
Agent session, but when the declared conversation or the photo changes its
history is cleared, so earlier images are never re-sent; corrections and continuations
for the same photo stay in the same conversation.  A photo whose typed reading was already persisted in this
run is reused through the existing task-attempt protocol and not sent again.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from manyselves.kernel.conversations import ConversationRecord
from manyselves.kernel.definitions import (
    AgentDefinition,
    RecoveryPolicyDefinition,
    TaskDefinition,
)
from manyselves.kernel.ports import AgentInvocationOutcome, AgentInvoker
from manyselves.runtime.agent_execution import (
    AgentExecutionService,
    AgentSessionLoop,
    AgentTurnRequest,
)
from manyselves.runtime.agent_recovery import AgentRecoveryDriver
from manyselves.runtime.loops.agent_loop import AgentLoop
from manyselves.runtime.provider_agent_session import ProviderAgentSessionFactory
from manyselves.runtime.services import RuntimeServicesView
from manyselves.runtime.tools.registry import ToolRegistry
from manyselves.runtime.typed_agent_turn import TypedAgentTurn

from .agent_recovery_turn import build_tool_recovery_callback, execute_reporting_recovery
from .agent_result_payload import load_agent_result_payload
from .collaboration_tools import ReportBlockedTool, SubmitResultTool
from .completed_result_recovery import ProviderTaskAttempt
from .contracts.submissions import submission_schema
from .models.agentic import AgentResult, PhotoReadingSubmission, TaskEnvelope
from .models.photo_reading import PhotoReadingInput
from .storage import ReportingStore

LoopBuilder = Callable[..., AgentSessionLoop]
SUBMISSION_KIND = "photo_reading_submission"


class PhotoReaderProviderRuntime:
    """Invoke the file-defined ``photo-reader`` Agent for one photo."""

    workflow_id = "public-reporting"

    def __init__(
        self,
        services: RuntimeServicesView,
        *,
        execution: AgentExecutionService | None = None,
        loop_builder: LoopBuilder = AgentLoop,
    ) -> None:
        if services.workspace is None:
            raise ValueError("photo reader composition requires a workspace")
        self.services = services
        self.workspace = Path(services.workspace).resolve()
        self.store = ReportingStore(self.workspace)
        self.execution = execution or AgentExecutionService(services.bus)
        self.loop_builder = loop_builder
        self.agent_invokers: Mapping[str, AgentInvoker] = {"photo-reader": self}
        self._session_conversations: dict[str, tuple[str, str]] = {}

    async def close(self) -> None:
        await self.execution.close_workflow(self.workflow_id)

    async def invoke(
        self, agent: AgentDefinition, task: TaskDefinition, value: Any,
        conversation: ConversationRecord, *, task_id: str,
    ) -> AgentInvocationOutcome:
        return await self._invoke(agent, task, value, conversation, task_id=task_id,
                                  recovery_policy=None)

    async def invoke_with_recovery(
        self, agent: AgentDefinition, task: TaskDefinition, value: Any,
        conversation: ConversationRecord, *, task_id: str,
        recovery_policy: RecoveryPolicyDefinition,
    ) -> AgentInvocationOutcome:
        return await self._invoke(agent, task, value, conversation, task_id=task_id,
                                  recovery_policy=recovery_policy)

    async def _invoke(
        self, agent: AgentDefinition, task: TaskDefinition, value: Any,
        conversation: ConversationRecord, *, task_id: str,
        recovery_policy: RecoveryPolicyDefinition | None,
    ) -> AgentInvocationOutcome:
        item = value if isinstance(value, PhotoReadingInput) else PhotoReadingInput.model_validate(
            value.model_dump(mode="json") if hasattr(value, "model_dump") else value
        )
        image = (self.workspace / item.photo_path).resolve()
        if not image.is_relative_to(self.workspace) or not image.is_file():
            return AgentInvocationOutcome(
                status="failed", error=f"photo is not a project-local file: {item.photo_path}",
            )
        envelope = self._envelope(agent, task, item)
        session_key = conversation.key.value
        session_id = f"{self.workflow_id}:{session_key}"
        attempt = ProviderTaskAttempt.acquire(
            self.workspace, envelope, workflow_id=self.workflow_id,
            identity_key=f"{agent.id}:{item.photo_id}", session_id=session_id,
        )
        try:
            completed = attempt.load_completed_or_activate()
            if completed is not None:
                return await self._reuse(completed, task, recovery_policy, session_id)
            recovery = AgentRecoveryDriver(recovery_policy) if recovery_policy is not None else None
            tools = self._tools(envelope, session_id, attempt, recovery, task)
            factory = ProviderAgentSessionFactory(
                loop_builder=self.loop_builder,
                loop_kwargs={
                    "agent_type": f"{self.workflow_id}:{agent.id}:{session_key}",
                    "workspace": self.workspace,
                    "tools": tools,
                    "bus": self.services.bus,
                    "config": self.services.agent_defaults,
                    "llm_provider": self.services.active_provider,
                    "system_prompt": agent.instructions,
                    "usage_run_id": item.run_id,
                    "usage_task_id": envelope.task_id,
                },
                persist_handoff_summary=False,
            )
            identity = (conversation.conversation_id, item.photo_id)
            existing = self.execution.session(self.workflow_id, session_key)
            if existing is not None:
                factory.reconfigure(existing.loop)
                if self._session_conversations.get(session_key) != identity:
                    _clear_history(existing.loop)
            self._session_conversations[session_key] = identity
            runtime_id = f"{self.workflow_id}:{agent.id}:{session_key}"
            turn = TypedAgentTurn(
                execution=self.execution, workflow_id=self.workflow_id,
                conversation_key=session_key, runtime_id=runtime_id,
                session_id=session_id, session_factory=factory,
            )
            session = await turn.start_or_restore()
            conversation.external_session_id = session.session_id
            attempt_id = attempt.correlation.task_attempt_id
            request = AgentTurnRequest(
                content=self._prompt(task, item),
                images=({"path": str(image), "media_type": item.media_type},),
                message_id=f"{envelope.task_id}:{item.run_id}:initial",
                workflow_id=self.workflow_id,
                run_id=item.run_id,
                task_id=task_id,
                task_attempt_id=attempt_id,
                turn_kind="task_initial",
            )
            terminal = turn.result_terminal(
                run_id=item.run_id, task_id=task_id, task_attempt_id=attempt_id,
                session_id=session.session_id, sender=envelope.agent_id,
                terminal_task_id=envelope.task_id, terminal_task_attempt_id=attempt_id,
            )
            if recovery_policy is None:
                outcome = await turn.dispatch(session, request, terminals=(terminal,))
                return TypedAgentTurn.map_outcome(
                    outcome, session_id=session.session_id, decode_result=self._decode,
                )
            recovered = await execute_reporting_recovery(
                self.execution, session, request,
                recovery_policy=recovery_policy, terminals=(terminal,),
                prompt_builder=lambda event: self._recovery_prompt(event),
                result_decoder=self._decode, recovery=recovery,
            )
            if isinstance(recovered, AgentInvocationOutcome):
                return recovered
            return AgentInvocationOutcome(status="ok", result=recovered, session_id=session.session_id)
        finally:
            attempt.close()

    async def _reuse(
        self, completed: AgentResult, task: TaskDefinition,
        recovery_policy: RecoveryPolicyDefinition | None, session_id: str,
    ) -> AgentInvocationOutcome:
        result = _submission_json(completed.payload)

        async def reuse(_directive: Any) -> AgentInvocationOutcome:
            return AgentInvocationOutcome(status="ok", result=result, session_id=completed.session_id)

        async def stop(directive: Any) -> AgentInvocationOutcome:
            return AgentInvocationOutcome(
                status="incomplete", session_id=session_id,
                error=getattr(directive, "reason", None) or "completed result recovery stopped",
            )

        if recovery_policy is None:
            return await reuse(None)
        recovered = await self.execution.recover_completed_result(
            recovery=AgentRecoveryDriver(recovery_policy),
            detail={"task_id": task.id, "source": "persisted_result"},
            reuse_result=reuse, stop=stop,
        )
        if isinstance(recovered, AgentInvocationOutcome):
            return recovered
        return await reuse(None)

    def _tools(
        self, envelope: TaskEnvelope, session_id: str, attempt: ProviderTaskAttempt,
        recovery: AgentRecoveryDriver | None, task: TaskDefinition,
    ) -> ToolRegistry:
        arguments = (envelope.agent_id, session_id, envelope.run_id, envelope.task_id,
                     self.store, self.services.bus, self.workflow_id)
        available = {
            "submit_result": SubmitResultTool(
                *arguments,
                allowed_outputs=[SUBMISSION_KIND],
                submission_schemas={SUBMISSION_KIND: submission_schema(SUBMISSION_KIND)},
                task_correlation=attempt.correlation,
                recovery_event_callback=(
                    build_tool_recovery_callback(recovery) if recovery is not None else None
                ),
            ),
            "report_blocked": ReportBlockedTool(*arguments, task_correlation=attempt.correlation),
        }
        registry = ToolRegistry()
        for name in task.tools:
            if name not in available:
                raise ValueError(f"unsupported photo reader tool: {name}")
            registry.register(available[name])
        if registry.get("submit_result") is not None:
            registry._schema_cache["submit_result"] = submission_schema(SUBMISSION_KIND)
        return registry

    @staticmethod
    def _envelope(agent: AgentDefinition, task: TaskDefinition, item: PhotoReadingInput) -> TaskEnvelope:
        return TaskEnvelope(
            task_id=f"photo-reading-{item.photo_id}",
            run_id=item.run_id,
            agent_id=agent.id,
            objective=task.objective,
            input_refs=[item.photo_path],
            constraints=list(task.constraints),
            allowed_outputs=[SUBMISSION_KIND],
            allowed_tools=list(task.tools),
            inline_context=json.dumps(item.model_dump(mode="json"), ensure_ascii=False, sort_keys=True),
        )

    @staticmethod
    def _prompt(task: TaskDefinition, item: PhotoReadingInput) -> str:
        records = [
            {
                "evidence_id": record.evidence_id,
                "对象": record.subject,
                "表格记录": record.fact,
                "位置": f"{record.sheet or ''}!{record.cell or ''}",
                "小节": record.submodule_id,
                "主记录": record.primary,
            }
            for record in item.records
        ]
        return "\n\n".join((
            f"任务：{task.objective}",
            f"图片 {item.photo_id} 已随本消息发送。它绑定的表格记录（主记录在前）：",
            json.dumps(records, ensure_ascii=False, indent=2),
            "看完图片后调用 submit_result 提交 photo_reading_submission。",
        ))

    @staticmethod
    def _recovery_prompt(event: Any) -> str:
        name = getattr(event, "value", str(event))
        return (
            f"<{name}>上一轮没有提交结构化结果。不要重新描述过程，"
            f"直接调用 submit_result 提交 photo_reading_submission。</{name}>"
        )

    def _decode(self, result_ref: str) -> dict[str, Any]:
        return _submission_json(load_agent_result_payload(self.workspace, result_ref).payload)


def _submission_json(payload: Any) -> dict[str, Any]:
    data = payload.model_dump(mode="json") if hasattr(payload, "model_dump") else payload
    return PhotoReadingSubmission.model_validate(data).model_dump(mode="json")


def _clear_history(loop: AgentSessionLoop) -> None:
    """Start a reset conversation with an empty history so earlier images are not re-sent."""

    try:
        loop.restore_conversation(())
    except RuntimeError:
        # A loop that is still active keeps its history; the next turn waits for it.
        pass


def build_photo_reader_provider(
    services: RuntimeServicesView,
    *,
    execution: AgentExecutionService | None = None,
    loop_builder: LoopBuilder = AgentLoop,
) -> PhotoReaderProviderRuntime:
    return PhotoReaderProviderRuntime(services, execution=execution, loop_builder=loop_builder)


__all__ = ["PhotoReaderProviderRuntime", "build_photo_reader_provider"]

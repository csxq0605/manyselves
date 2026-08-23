"""Capability-owned bridge for one typed module Author Agent turn.

The bridge adapts the file-defined module Author task to the neutral
``AgentExecutionService``.  This first slice deliberately ends after one
typed terminal is decoded; continuation, correction, and recovery remain
owned by later Capability runtime slices.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from manyselves.capabilities.distribution_reporting.runtime.models.agentic import (
    ModuleSubmission,
)
from manyselves.capabilities.distribution_reporting.runtime.models.module_lane import (
    DeclarativeModuleAuthoringAgentResult,
    DeclarativeModuleRuntimeLaneContext,
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


class ModuleAuthoringAgentBridge:
    """Invoke one module Author turn through the generic Agent service."""

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
        workflow_id = context.workflow_id or self.workflow_id
        run_id = str(context.reporting_state.get("run_id", ""))
        runtime_id = self._runtime_id(agent, conversation, workflow_id)
        session_id = conversation.external_session_id or (
            f"{workflow_id}:{conversation.key.value}"
        )
        try:
            session = await self.execution.start_or_restore(
                workflow_id=workflow_id,
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
            task_attempt_id=task_id,
            turn_kind="task_initial",
        )
        terminal = AgentTerminalSubscription(
            kind="typed_result",
            message_type=AgentResultMessage,
            predicate=lambda item: self._matches_result(
                item,
                workflow_id=workflow_id,
                runtime_id=runtime_id,
                run_id=run_id,
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
            result = DeclarativeModuleAuthoringAgentResult(
                status="completed",
                module=submission,
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

    def _read_submission(self, result_ref: str) -> ModuleSubmission:
        path = Path(result_ref)
        path = path if path.is_absolute() else self.workspace / path
        return ModuleSubmission.model_validate(
            json.loads(path.read_text(encoding="utf-8"))
        )

    def _runtime_id(
        self,
        agent: AgentDefinition,
        conversation: ConversationRecord,
        workflow_id: str,
    ) -> str:
        return f"{workflow_id}:{agent.id}:{conversation.key.value}"

    @staticmethod
    def _matches_result(
        item: AgentResultMessage,
        *,
        workflow_id: str,
        runtime_id: str,
        run_id: str,
        task_id: str,
        session_id: str,
    ) -> bool:
        return (
            item.sender == runtime_id
            and item.workflow_id == workflow_id
            and item.run_id == run_id
            and item.task_id == task_id
            and item.task_attempt_id == task_id
            and item.session_id == session_id
        )


__all__ = ["ModuleAuthoringAgentBridge"]

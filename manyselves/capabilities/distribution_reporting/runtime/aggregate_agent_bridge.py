"""Capability-owned bridge for one typed aggregate-editor Agent turn."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from manyselves.capabilities.distribution_reporting.runtime.agent_result_payload import (
    load_agent_result_payload,
)
from manyselves.capabilities.distribution_reporting.runtime.models.agentic import (
    EditedReportSubmission,
)
from manyselves.capabilities.distribution_reporting.runtime.models.inputs import (
    AggregateEditorInput,
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


class AggregateEditorAgentBridge:
    """Invoke one aggregate-editor turn through the neutral Agent service.

    The bridge owns only the aggregate Capability's input prompt and typed
    result decoder.  Session lifecycle, dispatch, and terminal correlation are
    delegated to ``AgentExecutionService`` and ``TypedAgentTurn``.
    """

    def __init__(
        self,
        workspace: Path,
        *,
        execution: AgentExecutionService,
        session_factory: SessionFactory,
        workflow_id: str = "distribution-aggregate-existing-tail",
        terminal_sender: str | None = None,
        terminal_task_id: str | None = None,
        terminal_task_attempt_id: str | None = None,
    ) -> None:
        self.workspace = Path(workspace).resolve()
        self.execution = execution
        self.session_factory = session_factory
        self.workflow_id = workflow_id
        self.terminal_sender = terminal_sender
        self.terminal_task_id = terminal_task_id
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
        """Keep the declared recovery port while this slice remains one-turn."""

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
        contract = (
            value
            if isinstance(value, AggregateEditorInput)
            else AggregateEditorInput.model_validate(value)
        )
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
            content=self._prompt(agent, task, contract),
            message_id=f"{task_id}:{contract.run_id}:initial",
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
            sender=self.terminal_sender,
            terminal_task_id=self.terminal_task_id,
            terminal_task_attempt_id=self.terminal_task_attempt_id,
        )
        outcome = await typed_turn.dispatch(
            session,
            request,
            terminals=(terminal,),
        )
        return TypedAgentTurn.map_outcome(
            outcome,
            session_id=session.session_id,
            decode_result=self._decode_result,
        )

    @staticmethod
    def _prompt(
        agent: AgentDefinition,
        task: TaskDefinition,
        value: AggregateEditorInput,
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

    def _decode_result(self, result_ref: str) -> dict[str, Any]:
        submission = EditedReportSubmission.model_validate(
            load_agent_result_payload(self.workspace, result_ref).payload
        )
        return submission.model_dump(mode="json")

    def _runtime_id(
        self,
        agent: AgentDefinition,
        conversation: ConversationRecord,
    ) -> str:
        return f"{self.workflow_id}:{agent.id}:{conversation.key.value}"


__all__ = ["AggregateEditorAgentBridge"]

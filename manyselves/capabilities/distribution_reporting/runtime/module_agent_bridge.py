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
from typing import Any, cast

from manyselves.capabilities.distribution_reporting.runtime.agent_result_payload import (
    load_agent_result_payload,
)
from manyselves.capabilities.distribution_reporting.runtime.models.agentic import (
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
from manyselves.runtime.typed_agent_turn import TypedAgentTurn

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
        envelope = cast(TaskEnvelope, self._envelope(context))
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
            content=self._prompt(agent, task, context),
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
        if output_contract == "declarative_module_revision_agent_result":
            submission = ModuleRevisionSubmission.model_validate(
                loaded.payload
            )
            return DeclarativeModuleRevisionAgentResult(
                status="completed",
                submission=submission,
            ).model_dump(mode="json")
        submission = ModuleSubmission.model_validate(loaded.payload)
        return DeclarativeModuleAuthoringAgentResult(
            status="completed",
            module=submission,
        ).model_dump(mode="json")

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

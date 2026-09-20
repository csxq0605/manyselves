"""Characterization for Capability-owned module Reviewer recovery."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from manyselves.capabilities.distribution_reporting.domain.taxonomy import REPORT_TAXONOMY
from manyselves.capabilities.distribution_reporting.runtime.models.agentic import (
    AgentResult,
    AgentRunStatus,
    ModuleReviewFindingSubmission,
    TaskEnvelope,
)
from manyselves.capabilities.distribution_reporting.runtime.models.inputs import (
    ModuleReviewInput,
)
from manyselves.capabilities.distribution_reporting.runtime.models.module_lane import (
    DeclarativeModuleReviewPreparation,
    DeclarativeModuleRuntimeLaneContext,
)
from manyselves.capabilities.distribution_reporting.runtime.models.review import (
    ModuleInitialReviewPreparation,
)
from manyselves.interfaces.types import AgentResponse, AgentResultMessage, UserMessage
from manyselves.kernel.conversations import ConversationKey, ConversationRegistry
from manyselves.kernel.definitions import (
    AgentDefinition,
    RecoveryPolicyDefinition,
    RecoveryRule,
    TaskDefinition,
)
from manyselves.runtime.agent_execution import AgentExecutionService
from manyselves.runtime.loops.agent_loop import (
    AGENT_MAX_TOKENS_CONTINUATION_REQUIRED,
    AGENT_TURN_CONTINUATION_REQUIRED,
)
from manyselves.runtime.loops.bus import MessageBus


def _review_context(run_id: str) -> tuple[AgentDefinition, TaskDefinition, DeclarativeModuleRuntimeLaneContext]:
    agent = AgentDefinition(
        id="evidence-auditor",
        version="1.0.0",
        description="module reviewer",
        instructions="review the module",
        accepts=["module_review_input"],
        produces=["module_review_finding_submission"],
    )
    task = TaskDefinition(
        id="module-2.4-review",
        version="1.0.0",
        description="module review task",
        agent=agent.id,
        objective="Review module 2.4",
        input_contract="module_review_input",
        output_contract="declarative_module_review_agent_result",
    )
    submodule_id = next(iter(REPORT_TAXONOMY["2.4"].submodules))
    envelope = TaskEnvelope(
        task_id=f"{run_id}-review-task",
        run_id=run_id,
        agent_id=agent.id,
        objective=task.objective,
        allowed_outputs=["module_review_finding_submission"],
        target_submodule_ids=[submodule_id],
    )
    review_input = ModuleReviewInput.model_construct(
        kind="module_review_input",
        phase="initial",
        run_id=run_id,
        module_id="2.4",
        lifecycle_id="initial",
        review_round=0,
        subject_ref=f"Work/runs/{run_id}/module.md",
        subject_revision=0,
        subject=None,
        evidence=[],
        required_submodule_ids=[submodule_id],
        validation_report_ref=f"Work/runs/{run_id}/validation.json",
        validation_report=None,
    )
    prepared = ModuleInitialReviewPreparation.model_construct(
        mode="invoke_agent",
        run_id=run_id,
        module_id="2.4",
        lifecycle_id="initial",
        workflow_id="public-reporting",
        reviewer_session_key="module-2.4-reviewer",
        review_root=f"Work/runs/{run_id}/reviews/2.4",
        progress_ref=f"Work/runs/{run_id}/reviews/2.4/progress.json",
        review_round=0,
        scope=[submodule_id],
        current=None,
        review_input=review_input,
        envelope=envelope,
    )
    review = DeclarativeModuleReviewPreparation.model_construct(
        envelope=envelope,
        reviewer_session_key="module-2.4-reviewer",
        prepared=prepared,
    )
    context = DeclarativeModuleRuntimeLaneContext.model_construct(
        module_id="2.4",
        workflow_id="public-reporting",
        reporting_state={"run_id": run_id},
        status="review_ready",
        review=review,
    )
    return agent, task, context


def _submission(submodule_id: str) -> ModuleReviewFindingSubmission:
    return ModuleReviewFindingSubmission(
        coverage={"submodule_ids": [submodule_id]},
        findings=[],
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("marker", "event_name", "continuation_kind", "prompt_fragment"),
    [
        (
            "natural-language",
            "natural_language_without_submission",
            "submission_correction",
            "submission_correction",
        ),
        (
            AGENT_MAX_TOKENS_CONTINUATION_REQUIRED,
            "max_tokens",
            "max_tokens_continuation",
            "max_tokens",
        ),
        (
            AGENT_TURN_CONTINUATION_REQUIRED,
            "tool_slice_boundary",
            "tool_slice_continuation",
            "tool_slice_boundary",
        ),
    ],
)
async def test_module_reviewer_bridge_recovers_in_same_session(
    tmp_path: Path,
    marker: str,
    event_name: str,
    continuation_kind: str,
    prompt_fragment: str,
) -> None:
    """Reviewer recovery keeps one Agent session and decodes a typed result."""

    from manyselves.capabilities.distribution_reporting.runtime.module_reviewer_bridge import (
        ModuleReviewerAgentBridge,
    )

    run_id = f"module-review-recovery-{event_name}"
    agent, task, context = _review_context(run_id)
    submodule_id = next(iter(REPORT_TAXONOMY["2.4"].submodules))
    result_ref = f"Work/runs/{run_id}/reviews/agent-result.json"
    result_path = tmp_path / result_ref
    result_path.parent.mkdir(parents=True, exist_ok=True)
    result_path.write_text(
        AgentResult(
            task_id=context.review.prepared.envelope.task_id,
            run_id=run_id,
            agent_id=agent.id,
            session_id="review-session",
            status=AgentRunStatus.COMPLETED,
            payload=_submission(submodule_id),
        ).model_dump_json(),
        encoding="utf-8",
    )

    bus = MessageBus()
    bus_task = asyncio.create_task(bus.process_queue())

    class ScriptedReviewerLoop:
        def __init__(self, runtime_id: str) -> None:
            self.runtime_id = runtime_id
            self.received: list[UserMessage] = []
            self._callback = None

        def restore_conversation(
            self,
            messages,
            *,
            task_boundaries=(),
            handoff_summary=None,
        ) -> None:
            del messages, task_boundaries, handoff_summary

        async def start(self) -> None:
            async def respond(message: UserMessage) -> None:
                if message.agent_type != self.runtime_id:
                    return
                self.received.append(message)
                if message.turn_kind == "task_initial":
                    await bus.publish(
                        AgentResponse(
                            agent_type=self.runtime_id,
                            message_id=message.message_id,
                            content=marker,
                            internal=marker.startswith("AGENT_"),
                            workflow_id=message.workflow_id,
                            run_id=message.run_id,
                            task_id=message.task_id,
                            task_attempt_id=message.task_attempt_id,
                            session_id=message.session_id,
                        )
                    )
                    return
                await bus.publish(
                    AgentResultMessage(
                        sender=agent.id,
                        workflow_id=message.workflow_id,
                        task_id=context.review.prepared.envelope.task_id,
                        run_id=message.run_id,
                        result_path=result_ref,
                        task_attempt_id="",
                        session_id=message.session_id,
                    )
                )

            self._callback = respond
            bus.subscribe(UserMessage, respond)

        async def stop(self) -> None:
            if self._callback is not None:
                bus.unsubscribe(UserMessage, self._callback)

        async def wait_until_turn_complete(self) -> None:
            return None

    loops: list[ScriptedReviewerLoop] = []

    def session_factory(runtime_id: str) -> ScriptedReviewerLoop:
        loop = ScriptedReviewerLoop(runtime_id)
        loops.append(loop)
        return loop

    conversation = ConversationRegistry().create_or_resolve(
        ConversationKey(
            agent_id=agent.id,
            value="module-2.4-reviewer",
            mode="run",
        ),
        run_id=run_id,
    )
    service = AgentExecutionService(bus, timeout=1)
    policy = RecoveryPolicyDefinition(
        id=f"review-{event_name}",
        version="1.0.0",
        description="module reviewer recovery",
        rules={event_name: RecoveryRule(action="correct" if event_name == "natural_language_without_submission" else "continue")},
    )
    bridge = ModuleReviewerAgentBridge(
        tmp_path,
        execution=service,
        session_factory=session_factory,
    )
    try:
        outcome = await bridge.invoke_with_recovery(
            agent,
            task,
            context,
            conversation,
            task_id=f"dispatch-{event_name}",
            recovery_policy=policy,
        )
    finally:
        await service.close_workflow("public-reporting")
        bus.shutdown()
        await bus_task

    assert outcome.status == "ok"
    assert outcome.result["status"] == "completed"
    assert outcome.result["submission"]["kind"] == "module_review_finding_submission"
    assert len(loops) == 1
    assert [message.turn_kind for message in loops[0].received] == [
        "task_initial",
        continuation_kind,
    ]
    assert loops[0].received[0].session_id == loops[0].received[1].session_id
    assert loops[0].received[1].internal is True
    assert prompt_fragment in loops[0].received[1].content
    assert outcome.session_id == conversation.external_session_id

"""Characterization for Capability-owned Chief chapter recovery."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from manyselves.capabilities.distribution_reporting.domain.taxonomy import REPORT_TAXONOMY
from manyselves.capabilities.distribution_reporting.runtime.models.agentic import (
    CHIEF_SECTION_RESULT_PART_IDS,
    AgentResult,
    AgentRunStatus,
    ChiefChapterLaneSubmission,
)
from manyselves.capabilities.distribution_reporting.runtime.models.reporting import (
    REPORT_MODULE_IDS,
)
from manyselves.interfaces.types import AgentResponse, AgentResultMessage, UserMessage
from manyselves.kernel.conversations import (
    ConversationKey,
    ConversationMode,
    ConversationRecord,
)
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


def _state(run_id: str) -> dict[str, object]:
    return {
        "run_id": run_id,
        "cross_review_completion_ref": (
            f"Work/runs/{run_id}/reviews/cross-completion.json"
        ),
        "cross_synthesis_inputs": [],
        "module_submissions": {
            module_id: {
                "module_id": module_id,
                "submodule_narratives": {
                    submodule_id: f"approved {module_id} {submodule_id}"
                    for submodule_id in REPORT_TAXONOMY[module_id].submodules
                },
                "claims": [],
                "source_ids": [],
                "unresolved_questions": [],
                "revision": 0,
            }
            for module_id in REPORT_MODULE_IDS
        },
        "preparation_refs": {
            "evidence": f"Work/runs/{run_id}/evidence.jsonl",
        },
        "template_skill_text": {
            "chief-editor-chapter-1": "Write only the assigned Chapter 1 sections.",
        },
    }


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
async def test_chief_agent_recovery_reuses_one_session(
    tmp_path: Path,
    marker: str,
    event_name: str,
    continuation_kind: str,
    prompt_fragment: str,
) -> None:
    """Chief recovery keeps one Agent session and decodes a typed result."""

    from manyselves.capabilities.distribution_reporting.runtime.chief_runtime import (
        ChiefChapterRuntime,
    )

    run_id = f"chief-recovery-{event_name}"
    result_ref = f"Work/runs/{run_id}/reviews/agent-result.json"
    result_path = tmp_path / result_ref
    result_path.parent.mkdir(parents=True, exist_ok=True)
    submission = ChiefChapterLaneSubmission(
        run_id=run_id,
        chapter_id="1",
        section_ids=["1.1", "1.2", "1.3"],
        part_refs={
            CHIEF_SECTION_RESULT_PART_IDS[section_id]: f"drafts/{section_id}.md"
            for section_id in ("1.1", "1.2", "1.3")
        },
        revision=0,
    )
    result_path.write_text(
        AgentResult(
            task_id="chief-chapter-1",
            run_id=run_id,
            agent_id="chief-editor",
            session_id="chief-recovery-session",
            status=AgentRunStatus.COMPLETED,
            payload=submission,
        ).model_dump_json(),
        encoding="utf-8",
    )

    bus = MessageBus()
    bus_task = asyncio.create_task(bus.process_queue())

    class ScriptedChiefLoop:
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
                        sender="chief-editor",
                        workflow_id=message.workflow_id,
                        task_id="chief-chapter-1",
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

    loops: list[ScriptedChiefLoop] = []

    def session_factory(runtime_id: str) -> ScriptedChiefLoop:
        loop = ScriptedChiefLoop(runtime_id)
        loops.append(loop)
        return loop

    conversation = ConversationRecord(
        conversation_id="chief-recovery-conversation",
        key=ConversationKey(
            agent_id="chief-editor",
            value="chief-chapter-1",
            mode=ConversationMode.RUN,
        ),
        run_id=run_id,
    )
    agent = AgentDefinition(
        id="chief-editor",
        version="1.0.0",
        description="Chief",
        instructions="Edit the assigned chapter.",
    )
    task = TaskDefinition(
        id="chief-chapter-edit",
        version="1.0.0",
        description="Chief lane",
        agent="chief-editor",
        objective="Edit one chapter.",
        input_contract="declarative_chief_chapter_context",
        output_contract="declarative_chief_chapter_agent_result",
        tools=["write_result_part", "list_result_parts", "submit_result"],
    )
    service = AgentExecutionService(bus, timeout=1)
    policy = RecoveryPolicyDefinition(
        id=f"chief-{event_name}",
        version="1.0.0",
        description="Chief chapter recovery",
        rules={
            event_name: RecoveryRule(
                action="correct"
                if event_name == "natural_language_without_submission"
                else "continue"
            )
        },
    )
    runtime = ChiefChapterRuntime(
        tmp_path,
        state=_state(run_id),
        agent_execution=service,
        agent_session_factory=session_factory,
        workflow_id="chief-workflow",
    )
    context = runtime.prepare_lane(
        {"state": runtime.current_state, "chapter_id": "1"}
    )
    try:
        outcome = await runtime.agent_invokers["chief-editor"].invoke_with_recovery(
            agent,
            task,
            context.model_dump(mode="json"),
            conversation,
            task_id=f"dispatch-{event_name}",
            recovery_policy=policy,
        )
    finally:
        await service.close_workflow("chief-workflow")
        bus.shutdown()
        await bus_task

    assert outcome.status == "ok"
    assert outcome.result["status"] == "completed"
    assert outcome.result["submission"]["chapter_id"] == "1"
    assert len(loops) == 1
    assert [message.turn_kind for message in loops[0].received] == [
        "task_initial",
        continuation_kind,
    ]
    assert loops[0].received[0].session_id == loops[0].received[1].session_id
    assert loops[0].received[1].internal is True
    assert prompt_fragment in loops[0].received[1].content
    assert outcome.session_id == conversation.external_session_id

"""Characterization for the Capability-owned initial Chief chapter runtime."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from manyselves.capabilities.distribution_reporting.domain.taxonomy import (
    REPORT_TAXONOMY,
)
from manyselves.capabilities.distribution_reporting.runtime.models.agentic import (
    CHIEF_SECTION_RESULT_PART_IDS,
    AgentResult,
    AgentRunStatus,
    ChiefChapterLaneSubmission,
    ModuleSubmission,
)
from manyselves.capabilities.distribution_reporting.runtime.models.reporting import (
    REPORT_MODULE_IDS,
)
from manyselves.core.loops.bus import MessageBus
from manyselves.interfaces.types import AgentResultMessage, UserMessage
from manyselves.kernel.conversations import (
    ConversationKey,
    ConversationMode,
    ConversationRecord,
)
from manyselves.kernel.definitions import AgentDefinition, TaskDefinition
from manyselves.runtime.agent_execution import AgentExecutionService


def _state(run_id: str) -> dict[str, object]:
    return {
        "run_id": run_id,
        "cross_review_completion_ref": (
            f"Work/runs/{run_id}/reviews/cross-completion.json"
        ),
        "cross_synthesis_inputs": [],
        "module_submissions": {
            module_id: ModuleSubmission(
                module_id=module_id,
                submodule_narratives={
                    submodule_id: f"approved {module_id} {submodule_id}"
                    for submodule_id in REPORT_TAXONOMY[module_id].submodules
                },
                claims=[],
                source_ids=[],
                unresolved_questions=[],
                revision=0,
            )
            for module_id in REPORT_MODULE_IDS
        },
        "preparation_refs": {
            "evidence": f"Work/runs/{run_id}/evidence.jsonl",
        },
        "template_skill_text": {
            "chief-editor-chapter-1": "Write only the assigned Chapter 1 sections.",
        },
    }


def test_chief_runtime_initial_preparation_preserves_lane_scope_and_inline_skill(
    tmp_path: Path,
) -> None:
    from manyselves.capabilities.distribution_reporting.runtime.chief_runtime import (
        ChiefChapterRuntime,
    )

    runtime = ChiefChapterRuntime(tmp_path, state=_state("chief-runtime-prep"))

    context = runtime.prepare_lane(
        {"state": runtime.current_state, "chapter_id": "1"}
    )

    assert context.status == "ready"
    assert context.contract is not None
    assert context.contract.chapter_id == "1"
    assert context.contract.section_ids == ["1.1", "1.2", "1.3"]
    assert context.contract.source_refs == [
        "Work/runs/chief-runtime-prep/reviews/cross-completion.json",
        "Work/runs/chief-runtime-prep/evidence.jsonl",
    ]
    assert set(context.contract.source_context) == {
        *(f"module-{module_id}" for module_id in REPORT_MODULE_IDS),
        "approved_markers",
    }
    assert context.contract.source_context["approved_markers"] == ",".join(
        f"[[APPROVED_MODULE:{module_id}]]" for module_id in REPORT_MODULE_IDS
    )
    assert context.envelope is not None
    assert context.envelope.task_id == "chief-chapter-1"
    assert context.envelope.run_id == "chief-runtime-prep"
    assert context.envelope.agent_id == "chief-editor"
    assert context.envelope.input_refs == [
        "Work/runs/chief-runtime-prep/context/chief-chapter-1-input.json"
    ]
    assert context.envelope.allowed_outputs == ["chief_chapter_lane_submission"]
    assert context.envelope.allowed_tools == [
        "write_result_part",
        "list_result_parts",
        "submit_result",
    ]
    assert context.envelope.revision == 0
    assert context.envelope.target_submodule_ids == []
    assert context.envelope.input_contract_kind == "chief_chapter_lane_input"
    assert context.envelope.input_contract_ref == context.envelope.input_refs[0]
    assert context.envelope.artifact_delivery_modes == {
        context.envelope.input_refs[0]: "inline"
    }
    assert "Write only the assigned Chapter 1 sections." in (
        context.envelope.inline_context or ""
    )

    assert context.contract.source_context
    assert context.contract.source_refs


def test_chief_runtime_accepts_lanes_and_reduces_initial_candidate(
    tmp_path: Path,
) -> None:
    from manyselves.capabilities.distribution_reporting.runtime.chief_runtime import (
        ChiefChapterRuntime,
    )

    run_id = "chief-runtime-reduce"
    runtime = ChiefChapterRuntime(tmp_path, state=_state(run_id))
    outcomes: dict[str, object] = {}
    for chapter_id, section_ids in {
        "1": ("1.1", "1.2", "1.3"),
        "3": ("3.1.1", "3.1.2", "3.1.3", "3.2"),
    }.items():
        context = runtime.prepare_lane(
            {"state": runtime.current_state, "chapter_id": chapter_id}
        )
        task_root = (
            tmp_path
            / "Work"
            / "runs"
            / run_id
            / "drafts"
            / f"chief-chapter-{chapter_id}"
            / "r0"
        )
        task_root.mkdir(parents=True, exist_ok=True)
        part_refs: dict[str, str] = {}
        for section_id in section_ids:
            part_id = CHIEF_SECTION_RESULT_PART_IDS[section_id]
            ref = (
                f"Work/runs/{run_id}/drafts/chief-chapter-{chapter_id}/r0/"
                f"{part_id}.md"
            )
            (tmp_path / ref).write_text(
                f"Chief body for {section_id}", encoding="utf-8"
            )
            part_refs[part_id] = ref
        submission = ChiefChapterLaneSubmission(
            run_id=run_id,
            chapter_id=chapter_id,
            section_ids=list(section_ids),
            part_refs=part_refs,
            revision=0,
        )
        result = {
            "status": "completed",
            "submission": submission.model_dump(mode="json"),
        }
        accepted_context = runtime.accept_lane(
            {
                "context": context.model_dump(mode="json"),
                "result": result,
            }
        )
        outcomes[chapter_id] = runtime.complete_lane(accepted_context)
        assert accepted_context.status == "accepted"
        assert accepted_context.output_ref == (
            f"Work/runs/{run_id}/reviews/chief-chapter-lane-{chapter_id}-r0.json"
        )

    reduced = runtime.reduce(
        {
            "state": runtime.current_state,
            "outcomes": {
                chapter_id: outcome.model_dump(mode="json")
                for chapter_id, outcome in outcomes.items()
            },
        }
    )

    assert reduced["chief_candidate_ref"] == (
        f"Work/runs/{run_id}/edited-revisions/chief-r0.json"
    )
    assert reduced["chief_chapter_lane_refs"] == {
        chapter_id: (
            f"Work/runs/{run_id}/reviews/chief-chapter-lane-{chapter_id}-r0.json"
        )
        for chapter_id in ("1", "3")
    }
    assert reduced["edited_report"].assessment_background == "Chief body for 1.1"
    assert reduced["edited_report"].risk_panorama == "Chief body for 3.1.1"
    assert (tmp_path / reduced["chief_candidate_ref"]).is_file()


@pytest.mark.asyncio
async def test_chief_runtime_initial_agent_uses_generic_session_and_typed_result(
    tmp_path: Path,
) -> None:
    from manyselves.capabilities.distribution_reporting.runtime.chief_runtime import (
        ChiefChapterRuntime,
    )

    run_id = "chief-runtime-agent"
    result_ref = f"Work/runs/{run_id}/reviews/agent-result.json"
    result_path = tmp_path / result_ref
    result_path.parent.mkdir(parents=True)
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
            session_id="chief-workflow:chief-chapter-1",
            status=AgentRunStatus.COMPLETED,
            payload=submission,
        ).model_dump_json(),
        encoding="utf-8",
    )

    bus = MessageBus()
    bus_task = asyncio.create_task(bus.process_queue())
    loops: list[object] = []

    class Loop:
        def __init__(self, runtime_id: str) -> None:
            self.runtime_id = runtime_id
            self.received: list[UserMessage] = []
            self._callback = None

        def restore_conversation(self, messages, *, task_boundaries=(), handoff_summary=None):
            del messages, task_boundaries, handoff_summary

        async def start(self) -> None:
            async def respond(message: UserMessage) -> None:
                if message.agent_type != self.runtime_id:
                    return
                self.received.append(message)
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

    def session_factory(runtime_id: str) -> Loop:
        loop = Loop(runtime_id)
        loops.append(loop)
        return loop

    execution = AgentExecutionService(bus, timeout=1)
    runtime = ChiefChapterRuntime(
        tmp_path,
        state=_state(run_id),
        agent_execution=execution,
        agent_session_factory=session_factory,
        workflow_id="chief-workflow",
    )
    context = runtime.prepare_lane(
        {"state": runtime.current_state, "chapter_id": "1"}
    )
    conversation = ConversationRecord(
        conversation_id="chief-conversation",
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
    try:
        outcome = await runtime.agent_invokers["chief-editor"].invoke(
            agent,
            task,
            context.model_dump(mode="json"),
            conversation,
            task_id="invoke-chief-chapter-agent",
        )
    finally:
        await execution.close_workflow("chief-workflow")
        bus.shutdown()
        await bus_task

    assert outcome.status == "ok"
    assert outcome.session_id == "chief-workflow:chief-chapter-1"
    assert len(loops) == 1
    assert len(loops[0].received) == 1
    assert loops[0].received[0].task_id == "invoke-chief-chapter-agent"
    assert outcome.result["status"] == "completed"
    assert outcome.result["submission"]["chapter_id"] == "1"

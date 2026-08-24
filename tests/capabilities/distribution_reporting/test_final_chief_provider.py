"""Characterization for the Capability-owned Final Chief revision Provider path."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from manyselves.capabilities.distribution_reporting.runtime.agent_result_payload import (
    load_agent_result_payload,
)
from manyselves.capabilities.distribution_reporting.runtime.models.agentic import (
    AgentResult,
    AgentRunStatus,
    ChapterScopedFinalReviewFinding,
    ChapterScopedFinalReviewTargetChange,
    ChiefChapterLaneRevisionSubmission,
    ChiefSectionTextEdit,
    TaskEnvelope,
)
from manyselves.capabilities.distribution_reporting.runtime.models.final_review import (
    DeclarativeFinalChiefRevisionContext,
)
from manyselves.capabilities.distribution_reporting.runtime.models.inputs import (
    ChiefChapterLaneInput,
)
from manyselves.capabilities.distribution_reporting.runtime.storage import ReportingStore
from manyselves.config.schema import AgentDefaults
from manyselves.interfaces.types import UserMessage
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
from manyselves.runtime.loops.bus import MessageBus
from manyselves.runtime.services import RuntimeServicesView


def _context(tmp_path: Path, run_id: str) -> DeclarativeFinalChiefRevisionContext:
    source_refs = [
        f"Work/runs/{run_id}/reviews/cross-completion.json",
        f"Work/runs/{run_id}/evidence.jsonl",
    ]
    finding = ChapterScopedFinalReviewFinding(
        id="F-1-001",
        target_section_ids=["1.2"],
        target_changes=[
            ChapterScopedFinalReviewTargetChange(
                target_section_id="1.2",
                required_change="补充可核验的事实边界、责任主体与后续动作。",
                reviewer_checks=["章节明确给出事实边界、责任主体与后续动作"],
            )
        ],
        category="traceability",
        impact="blocking",
        observation="当前章节没有把行动责任与后续核验方式写清楚，读者无法直接执行。",
        evidence_refs=[f"Work/runs/{run_id}/edited-r0.json"],
    )
    contract = ChiefChapterLaneInput(
        phase="revision",
        run_id=run_id,
        subject_ref=f"Work/runs/{run_id}/edited-r0.json",
        chapter_id="1",
        section_ids=["1.2"],
        section_bodies={"1.2": "当前章节正文。"},
        source_refs=source_refs,
        assigned_findings=[finding],
        revision=1,
    )
    input_ref = f"Work/runs/{run_id}/context/chief-chapter-1-input-r1.json"
    store = ReportingStore(tmp_path)
    store.write_json(input_ref, contract.model_dump(mode="json"))
    store.write_json(contract.subject_ref, {"kind": "existing-edited-report"})
    store.write_json(source_refs[0], {"kind": "cross-completion"})
    evidence_path = tmp_path / source_refs[1]
    evidence_path.write_text(
        '{"id":"E-0001","content":"approved fact"}\n',
        encoding="utf-8",
    )
    envelope = TaskEnvelope(
        task_id="chief-chapter-1-r1",
        run_id=run_id,
        agent_id="chief-editor",
        objective="只修订 Chapter 1 被 Final 指定的 finding 小节。",
        input_refs=[input_ref, *source_refs],
        constraints=["只提交 chief_chapter_lane_revision_submission。"],
        allowed_outputs=["chief_chapter_lane_revision_submission"],
        allowed_tools=["open_artifact", "search_text", "submit_result"],
        revision=1,
        prior_result_ref=contract.subject_ref,
        input_contract_kind="chief_chapter_lane_input",
        input_contract_ref=input_ref,
        artifact_delivery_modes={
            input_ref: "inline",
            source_refs[0]: "reference",
            source_refs[1]: "reference",
            contract.subject_ref: "hash_retained",
        },
        inline_context="Final Chief revision skill: keep the patch inside the assigned lane.",
    )
    return DeclarativeFinalChiefRevisionContext(
        chapter_id="1",
        status="ready",
        contract=contract,
        input_ref=input_ref,
        envelope=envelope,
    )


def test_final_chief_revision_cannot_replace_a_complete_section_with_new_facts(
    tmp_path: Path,
) -> None:
    from manyselves.capabilities.distribution_reporting.runtime.final_review_tools import (
        FinalReviewTools,
    )

    context = _context(tmp_path, "final-chief-whole-section-drift")
    contract = ChiefChapterLaneInput.model_validate(context.contract)
    submission = ChiefChapterLaneRevisionSubmission(
        run_id=contract.run_id,
        base_subject_ref=contract.subject_ref,
        chapter_id="1",
        revision=1,
        section_ids=["1.2"],
        edits=[
            ChiefSectionTextEdit(
                target_section_id="1.2",
                old_text="当前章节正文。",
                new_text="XX主中心采用2N UPS，并配置柴油发电机。",
            )
        ],
    )

    with pytest.raises(ValueError, match="cannot replace an entire section"):
        FinalReviewTools._apply_chief_revision_edits(contract, submission)


@pytest.mark.asyncio
async def test_final_chief_provider_shares_declared_tool_and_agent_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from manyselves.capabilities.distribution_reporting.runtime import (
        final_chief_agent_bridge,
        final_chief_provider,
    )
    from manyselves.runtime.tools.registry import ToolRegistry

    captured: dict[str, object] = {}

    def capture_tools(*args, **kwargs):
        del args
        captured["dependencies"] = kwargs["dependencies"]
        captured["expected_part_ids"] = kwargs["expected_part_ids"]
        return ToolRegistry()

    async def capture_recovery(*args, **kwargs):
        del args
        captured["recovery"] = kwargs["recovery"]
        return {"status": "completed", "submission": {}}

    class Loop:
        def restore_conversation(self, messages, *, task_boundaries=(), handoff_summary=None):
            del messages, task_boundaries, handoff_summary

        async def start(self) -> None:
            return None

        async def stop(self) -> None:
            return None

        async def wait_until_turn_complete(self) -> None:
            return None

    monkeypatch.setattr(final_chief_provider, "build_module_provider_tools", capture_tools)
    monkeypatch.setattr(
        final_chief_agent_bridge,
        "execute_reporting_recovery",
        capture_recovery,
        raising=False,
    )
    bus = MessageBus()
    runtime = final_chief_provider.FinalChiefProviderRuntime(
        RuntimeServicesView(
            workspace=tmp_path,
            bus=bus,
            active_provider=object(),
            agent_defaults=AgentDefaults(),
            global_knowledge_root=None,
        ),
        loop_builder=lambda **kwargs: Loop(),
    )
    context = _context(tmp_path, "final-chief-provider-run")
    agent = AgentDefinition(
        id="chief-editor",
        version="1.0.0",
        description="Chief Editor",
        instructions="Revise the assigned chapter.",
        tools=["open_artifact", "search_text", "submit_result"],
    )
    task = TaskDefinition(
        id="final-chief-chapter-revision",
        version="1.0.0",
        description="Final Chief revision lane",
        agent=agent.id,
        objective="Revise only the assigned chapter finding.",
        input_contract="declarative_final_chief_revision_context",
        output_contract="declarative_final_chief_revision_agent_result",
        tools=["open_artifact", "search_text", "submit_result"],
    )
    policy = RecoveryPolicyDefinition(
        id="final-chief-schema-recovery",
        version="1.0.0",
        description="correct invalid structured output",
        rules={
            "invalid_structured_output": RecoveryRule(action="correct"),
        },
    )
    conversation = ConversationRecord(
        conversation_id="final-chief-schema-conversation",
        key=ConversationKey(
            agent_id=agent.id,
            value="chief-chapter-1",
            mode=ConversationMode.RUN,
        ),
        run_id="final-chief-provider-run",
    )

    bridge = runtime._bridge(
        agent,
        task,
        context,
        conversation,
        recovery_policy=policy,
    )
    dependencies = captured["dependencies"]
    assert captured["expected_part_ids"] == []
    decision = await dependencies.recovery_event_callback(
        "invalid_structured_output",
        {"task_id": task.id},
    )
    try:
        outcome = await bridge.invoke_with_recovery(
            agent,
            task,
            context,
            conversation,
            task_id="invoke-final-chief-action",
            recovery_policy=policy,
        )
    finally:
        await runtime.close()

    assert outcome.status == "ok"
    assert decision.action.value == "correct"
    assert captured["recovery"] is bridge.recovery_driver
    assert bridge.recovery_driver.snapshot_attempts() == {
        "invalid_structured_output": 1,
    }
    assert bridge.progress_observer is not None


@pytest.mark.asyncio
async def test_final_chief_provider_uses_real_revision_tools_and_reuses_one_session(
    tmp_path: Path,
) -> None:
    from manyselves.capabilities.distribution_reporting.runtime.final_chief_provider import (
        build_final_chief_provider_composition,
    )

    run_id = "final-chief-provider-run"
    bus = MessageBus()
    bus_task = asyncio.create_task(bus.process_queue())
    built: list[dict[str, object]] = []
    loops: list[object] = []
    context = _context(tmp_path, run_id)

    class Loop:
        def __init__(self, kwargs: dict[str, object]) -> None:
            self.kwargs = kwargs
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
                if message.agent_type != self.kwargs["agent_type"]:
                    return
                self.received.append(message)
                tools = self.kwargs["tools"]
                await tools.get("submit_result")(
                    kind="chief_chapter_lane_revision_submission",
                    run_id=run_id,
                    base_subject_ref=context.contract.subject_ref,
                    chapter_id="1",
                    revision=1,
                    section_ids=["1.2"],
                    edits=[
                        {
                            "target_section_id": "1.2",
                            "old_text": "当前章节正文。",
                            "new_text": (
                                "当前章节正文。\n\n"
                                "已补充事实边界、责任主体与后续动作，便于读者执行。"
                            ),
                        }
                    ],
                    revision_responses=[
                        {
                            "finding_id": "F-1-001",
                            "action": "implemented",
                            "summary": "已补充事实边界、责任主体与后续动作，便于执行。",
                            "changed_target_ids": ["1.2"],
                        }
                    ],
                )

            self._callback = respond
            bus.subscribe(UserMessage, respond)

        async def stop(self) -> None:
            if self._callback is not None:
                bus.unsubscribe(UserMessage, self._callback)

        async def wait_until_turn_complete(self) -> None:
            return None

    def loop_builder(**kwargs: object) -> Loop:
        built.append(kwargs)
        loop = Loop(kwargs)
        loops.append(loop)
        return loop

    services = RuntimeServicesView(
        workspace=tmp_path,
        bus=bus,
        active_provider=object(),
        agent_defaults=AgentDefaults(),
        global_knowledge_root=None,
    )
    composition = build_final_chief_provider_composition(
        services,
        execution=AgentExecutionService(bus, timeout=1),
        loop_builder=loop_builder,
    )
    conversation = ConversationRecord(
        conversation_id="final-chief-provider-conversation",
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
        description="Chief Editor",
        instructions="修订指定章节并提交 typed revision patch。",
        tools=["open_artifact", "search_text", "submit_result"],
    )
    task = TaskDefinition(
        id="final-chief-chapter-revision",
        version="1.0.0",
        description="Final Chief revision lane",
        agent=agent.id,
        objective="Revise only the assigned chapter finding.",
        input_contract="declarative_final_chief_revision_context",
        output_contract="declarative_final_chief_revision_agent_result",
        tools=["open_artifact", "search_text", "submit_result"],
    )
    try:
        first = await composition.agent_invokers[agent.id].invoke(
            agent,
            task,
            context.model_dump(mode="json"),
            conversation,
            task_id="invoke-final-chief-action",
        )
        second = await composition.agent_invokers[agent.id].invoke(
            agent,
            task,
            context.model_dump(mode="json"),
            conversation,
            task_id="invoke-final-chief-action-again",
        )
    finally:
        await composition.close()
        bus.shutdown()
        await bus_task

    assert first.status == "ok"
    assert second.status == "ok"
    assert len(loops) == 1
    assert len(built) == 1
    assert built[0]["llm_provider"] is services.active_provider
    assert set(loops[0].kwargs["tools"].get_all()) == {
        "open_artifact",
        "search_text",
        "submit_result",
    }
    assert [message.session_id for message in loops[0].received] == [
        conversation.external_session_id,
    ]
    result_path = tmp_path / f"Work/runs/{run_id}/results/chief-chapter-1-r1.json"
    loaded = load_agent_result_payload(tmp_path, result_path)
    assert isinstance(loaded.payload, ChiefChapterLaneRevisionSubmission)
    assert loaded.identity.task_id == "chief-chapter-1-r1"
    assert loaded.identity.agent_id == "chief-editor"
    assert loaded.identity.session_id == conversation.external_session_id


@pytest.mark.asyncio
async def test_final_chief_provider_reuses_persisted_completed_result_before_provider(
    tmp_path: Path,
) -> None:
    """The account Provider must load a verified Chief result before a session."""

    from manyselves.capabilities.distribution_reporting.runtime.completed_result_recovery import (
        build_task_correlation,
    )
    from manyselves.capabilities.distribution_reporting.runtime.final_chief_provider import (
        FinalChiefProviderRuntime,
    )
    from manyselves.capabilities.distribution_reporting.runtime.state.parallel import (
        IdentityLeaseManager,
        TaskAttemptStore,
    )

    run_id = "final-chief-provider-persisted-reuse"
    context = _context(tmp_path, run_id)
    agent = AgentDefinition(
        id="chief-editor",
        version="1.0.0",
        description="Chief editor",
        instructions="Revise the assigned chapter.",
        tools=["open_artifact", "search_text", "submit_result"],
    )
    task = TaskDefinition(
        id="final-chief-chapter-revision",
        version="1.0.0",
        description="Final Chief revision lane",
        agent=agent.id,
        objective="Revise only the assigned chapter finding.",
        input_contract="declarative_final_chief_revision_context",
        output_contract="declarative_final_chief_revision_agent_result",
        tools=["open_artifact", "search_text", "submit_result"],
    )
    workflow_id = "distribution-aggregate-existing-tail"
    identity_key = "chief-chapter-1"
    session_id = identity_key
    lease_handle = IdentityLeaseManager(tmp_path, run_id).acquire(
        workflow_id,
        identity_key,
    )
    correlation = build_task_correlation(
        tmp_path,
        context.envelope,
        workflow_id=workflow_id,
        identity_key=identity_key,
        session_id=session_id,
        identity_lease=lease_handle.lease,
    )
    submission = ChiefChapterLaneRevisionSubmission(
        run_id=run_id,
        base_subject_ref=context.contract.subject_ref,
        chapter_id="1",
        revision=1,
        section_ids=["1.2"],
        edits=[
            ChiefSectionTextEdit(
                target_section_id="1.2",
                old_text="当前章节正文。",
                new_text="当前章节正文。\n\n补充核验说明。",
            )
        ],
    )
    try:
        store = TaskAttemptStore(tmp_path, run_id)
        store.activate(correlation)
        store.persist_result(
            correlation,
            AgentResult(
                task_id=context.envelope.task_id,
                run_id=run_id,
                agent_id=agent.id,
                session_id=session_id,
                status=AgentRunStatus.COMPLETED,
                payload=submission,
            ).model_dump(mode="json"),
            status="completed",
        )
    finally:
        lease_handle.release()

    provider_calls = 0

    def forbidden_loop_builder(**_kwargs):
        nonlocal provider_calls
        provider_calls += 1
        raise AssertionError("completed Chief result must be reused before Provider session")

    bus = MessageBus()
    service = AgentExecutionService(bus)
    runtime = FinalChiefProviderRuntime(
        RuntimeServicesView(
            workspace=tmp_path,
            bus=bus,
            active_provider=object(),
            agent_defaults=AgentDefaults(),
            global_knowledge_root=None,
        ),
        execution=service,
        loop_builder=forbidden_loop_builder,
    )
    conversation = ConversationRecord(
        conversation_id="final-chief-provider-persisted-conversation",
        key=ConversationKey(
            agent_id=agent.id,
            value=identity_key,
            mode=ConversationMode.RUN,
        ),
        run_id=run_id,
    )
    outcome = await runtime.invoke(
        agent,
        task,
        context,
        conversation,
        task_id="host-final-chief-provider-persisted",
    )

    assert outcome.status == "ok"
    assert provider_calls == 0
    assert service.sessions == {}
    assert outcome.session_id == session_id

"""Characterization for the Capability-owned Chief Provider composition."""

from __future__ import annotations

import asyncio
import subprocess
import sys
from pathlib import Path

import pytest

from manyselves.capabilities.distribution_reporting.domain.taxonomy import (
    REPORT_TAXONOMY,
)
from manyselves.capabilities.distribution_reporting.runtime.chief_runtime import (
    ChiefChapterRuntime,
)
from manyselves.capabilities.distribution_reporting.runtime.models.agentic import (
    CHIEF_SECTION_RESULT_PART_IDS,
    ModuleSubmission,
)
from manyselves.capabilities.distribution_reporting.runtime.models.reporting import (
    REPORT_MODULE_IDS,
)
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


def _state(run_id: str) -> dict[str, object]:
    return {
        "run_id": run_id,
        "cross_review_completion_ref": (
            f"Work/runs/{run_id}/reviews/cross-completion.json"
        ),
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


def _write_source_artifacts(workspace: Path, run_id: str) -> None:
    cross_path = workspace / f"Work/runs/{run_id}/reviews/cross-completion.json"
    cross_path.parent.mkdir(parents=True, exist_ok=True)
    cross_path.write_text("{}\n", encoding="utf-8")
    evidence_path = workspace / f"Work/runs/{run_id}/evidence.jsonl"
    evidence_path.write_text(
        '{"id":"E-0001","content":"approved fact"}\n',
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_chief_provider_shares_declared_recovery_with_tool_callback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from manyselves.capabilities.distribution_reporting.runtime import chief_provider
    from manyselves.runtime.tools.registry import ToolRegistry

    captured: dict[str, object] = {}

    def capture_tools(*args, **kwargs):
        del args
        captured["dependencies"] = kwargs["dependencies"]
        return ToolRegistry()

    monkeypatch.setattr(chief_provider, "build_module_provider_tools", capture_tools)
    bus = MessageBus()
    runtime = chief_provider.ChiefProviderRuntime(
        RuntimeServicesView(
            workspace=tmp_path,
            bus=bus,
            active_provider=object(),
            agent_defaults=AgentDefaults(),
            global_knowledge_root=None,
        )
    )
    _write_source_artifacts(tmp_path, "chief-schema-run")
    chief_runtime = ChiefChapterRuntime(tmp_path, state=_state("chief-schema-run"))
    context = chief_runtime.prepare_lane(
        {"state": chief_runtime.current_state, "chapter_id": "1"}
    )
    agent = AgentDefinition(
        id="chief-editor",
        version="1.0.0",
        description="Chief",
        instructions="Edit the assigned chapter.",
        tools=[
            "open_artifact",
            "search_text",
            "write_result_part",
            "list_result_parts",
            "submit_result",
        ],
    )
    task = TaskDefinition(
        id="chief-chapter-edit",
        version="1.0.0",
        description="Chief lane",
        agent=agent.id,
        objective="Edit one chapter.",
        input_contract="declarative_chief_chapter_context",
        output_contract="declarative_chief_chapter_agent_result",
        tools=[
            "open_artifact",
            "search_text",
            "write_result_part",
            "list_result_parts",
            "submit_result",
        ],
    )
    policy = RecoveryPolicyDefinition(
        id="chief-schema-recovery",
        version="1.0.0",
        description="correct invalid structured output",
        rules={
            "invalid_structured_output": RecoveryRule(action="correct"),
        },
    )
    conversation = ConversationRecord(
        conversation_id="chief-schema-conversation",
        key=ConversationKey(
            agent_id=agent.id,
            value="chief-chapter-1",
            mode=ConversationMode.RUN,
        ),
        run_id="chief-schema-run",
    )

    bridge = runtime._bridge(
        agent,
        task,
        context,
        conversation,
        task_id="invoke-current-chief-chapter",
        recovery_policy=policy,
    )
    dependencies = captured["dependencies"]
    decision = await dependencies.recovery_event_callback(
        "invalid_structured_output",
        {"task_id": task.id},
    )

    assert decision.action.value == "correct"
    assert bridge.recovery_driver.snapshot_attempts() == {
        "invalid_structured_output": 1,
    }
    assert bridge.progress_observer is not None


@pytest.mark.asyncio
async def test_chief_provider_composition_uses_real_tools_and_one_provider_session(
    tmp_path: Path,
) -> None:
    from manyselves.capabilities.distribution_reporting.runtime.chief_provider import (
        build_chief_provider_composition,
    )

    run_id = "chief-provider-run"
    bus = MessageBus()
    bus_task = asyncio.create_task(bus.process_queue())
    loops: list[object] = []
    built: list[dict[str, object]] = []
    opened_sources: list[dict[str, object]] = []

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
                opened_sources.append(
                    await tools.get("open_artifact")(
                        f"Work/runs/{run_id}/context/chief-source-modules/2.1.md"
                    )
                )
                for section_id in ("1.1", "1.2", "1.3"):
                    await tools.get("write_result_part")(
                        CHIEF_SECTION_RESULT_PART_IDS[section_id],
                        f"Chief body for {section_id}",
                    )
                parts = await tools.get("list_result_parts")()
                await tools.get("submit_result")(
                    kind="chief_chapter_lane_submission",
                    run_id=run_id,
                    chapter_id="1",
                    section_ids=["1.1", "1.2", "1.3"],
                    part_refs={
                        part["part_id"]: part["artifact_ref"]
                        for part in parts["parts"]
                    },
                    revision=0,
                )

            self._callback = respond
            bus.subscribe(UserMessage, respond)

        async def stop(self) -> None:
            if self._callback is not None:
                bus.unsubscribe(UserMessage, self._callback)

        async def wait_until_turn_complete(self) -> None:
            return None

    def loop_builder(**kwargs):
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
    chief_runtime = ChiefChapterRuntime(tmp_path, state=_state(run_id))
    _write_source_artifacts(tmp_path, run_id)
    composition = build_chief_provider_composition(
        services,
        chief_runtime=chief_runtime,
        execution=AgentExecutionService(bus, timeout=1),
        loop_builder=loop_builder,
    )
    context = chief_runtime.prepare_lane(
        {"state": chief_runtime.current_state, "chapter_id": "1"}
    )
    conversation = ConversationRecord(
        conversation_id="chief-provider-conversation",
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
        tools=[
            "open_artifact",
            "search_text",
            "write_result_part",
            "list_result_parts",
            "submit_result",
        ],
    )
    task = TaskDefinition(
        id="chief-chapter-edit",
        version="1.0.0",
        description="Chief lane",
        agent="chief-editor",
        objective="Edit one chapter.",
        input_contract="declarative_chief_chapter_context",
        output_contract="declarative_chief_chapter_agent_result",
        tools=[
            "open_artifact",
            "search_text",
            "write_result_part",
            "list_result_parts",
            "submit_result",
        ],
    )
    try:
        outcome = await composition.agent_invokers["chief-editor"].invoke(
            agent,
            task,
            context.model_dump(mode="json"),
            conversation,
            task_id="invoke-current-chief-chapter",
        )
        second = await composition.agent_invokers["chief-editor"].invoke(
            agent,
            task,
            context.model_dump(mode="json"),
            conversation,
            task_id="invoke-current-chief-chapter",
        )
    finally:
        await composition.execution.close_workflow(composition.workflow_id)
        bus.shutdown()
        await bus_task

    assert outcome.status == "ok"
    assert second.status == "ok"
    assert len(loops) == 1
    assert len(built) == 1
    assert set(loops[0].kwargs["tools"].get_all()) == {
        "open_artifact",
        "open_tool_result",
        "search_text",
        "write_result_part",
        "list_result_parts",
        "submit_result",
    }
    assert set(context.envelope.input_refs) == {
        context.input_ref,
        f"Work/runs/{run_id}/reviews/cross-completion.json",
        f"Work/runs/{run_id}/evidence.jsonl",
        *{
            f"Work/runs/{run_id}/context/chief-source-modules/{module_id}.md"
            for module_id in REPORT_MODULE_IDS
        },
    }
    assert context.envelope.artifact_delivery_modes == {
        context.input_ref: "inline",
        f"Work/runs/{run_id}/reviews/cross-completion.json": "reference",
        f"Work/runs/{run_id}/evidence.jsonl": "reference",
        **{
            f"Work/runs/{run_id}/context/chief-source-modules/{module_id}.md": (
                "reference"
            )
            for module_id in REPORT_MODULE_IDS
        },
    }
    assert len(opened_sources) == 1
    assert "approved 2.1" in str(opened_sources[0]["content"])
    assert [message.session_id for message in loops[0].received] == [
        conversation.external_session_id,
    ]
    assert conversation.external_session_id == "public-reporting:chief-chapter-1"
    assert "Write only the assigned Chapter 1 sections." in loops[0].received[0].content

    import json
    from types import SimpleNamespace

    from manyselves.runtime.loops.agent_loop import AgentLoop

    # The real formatting boundary tells the Chief to reopen this opaque ref.
    # That reader is Runtime-owned, not an extra public artifact permission.
    formatter = SimpleNamespace(
        config=AgentDefaults(max_tool_result_chars=6000),
        agent_type="chief-editor",
        artifact_gateway=built[0]["artifact_gateway"],
    )
    full_text = "approved source detail " * 1000
    truncated = json.loads(AgentLoop._format_tool_result(
        formatter, full_text, tool_name="open_artifact", tool_call_id="large-source",
    ))
    assert truncated["truncated"]
    page = await loops[0].kwargs["tools"].get("open_tool_result")(
        truncated["full_result_ref"], offset=truncated["next_offset"],
    )
    assert page["content"] == full_text[truncated["next_offset"]:truncated["next_offset"] + 8000]
    with pytest.raises(PermissionError, match="not delivered"):
        await loops[0].kwargs["tools"].get("open_artifact")(truncated["full_result_ref"])


@pytest.mark.asyncio
async def test_chief_provider_reuses_persisted_completed_result_before_provider(
    tmp_path: Path,
) -> None:
    """A verified Chief completion must be reused before Provider session creation."""

    from manyselves.capabilities.distribution_reporting.runtime.chief_provider import (
        build_chief_provider_composition,
    )
    from manyselves.capabilities.distribution_reporting.runtime.completed_result_recovery import (
        ProviderTaskAttempt,
        reporting_identity_key,
    )
    from manyselves.capabilities.distribution_reporting.runtime.models.agentic import (
        AgentResult,
        AgentRunStatus,
        ChiefChapterLaneSubmission,
    )
    from manyselves.capabilities.distribution_reporting.runtime.state.parallel import (
        TaskAttemptStore,
    )

    run_id = "chief-provider-persisted-reuse"
    bus = MessageBus()
    _write_source_artifacts(tmp_path, run_id)
    chief_runtime = ChiefChapterRuntime(tmp_path, state=_state(run_id))
    chief_runtime.store.write_json(
        f"Work/runs/{run_id}/reviews/cross-completion.json",
        {},
    )
    context = chief_runtime.prepare_lane(
        {"state": chief_runtime.current_state, "chapter_id": "1"}
    )
    assert context.contract is not None
    assert context.envelope is not None
    envelope = context.envelope
    conversation = ConversationRecord(
        conversation_id="chief-provider-persisted-conversation",
        key=ConversationKey(
            agent_id="chief-editor",
            value="chief-chapter-1",
            mode=ConversationMode.RUN,
        ),
        run_id=run_id,
        external_session_id="persisted-chief-session",
    )
    agent = AgentDefinition(
        id="chief-editor",
        version="1.0.0",
        description="Chief",
        instructions="Edit the assigned chapter.",
        tools=[
            "open_artifact",
            "search_text",
            "write_result_part",
            "list_result_parts",
            "submit_result",
        ],
    )
    task = TaskDefinition(
        id="chief-chapter-edit",
        version="1.0.0",
        description="Chief lane",
        agent=agent.id,
        objective="Edit one chapter.",
        input_contract="declarative_chief_chapter_context",
        output_contract="declarative_chief_chapter_agent_result",
        tools=[
            "open_artifact",
            "search_text",
            "write_result_part",
            "list_result_parts",
            "submit_result",
        ],
    )
    identity_key = reporting_identity_key(agent.id, conversation.key.value)
    attempt = ProviderTaskAttempt.acquire(
        tmp_path,
        envelope,
        workflow_id="public-reporting",
        identity_key=identity_key,
        session_id=conversation.external_session_id,
    )
    persisted = AgentResult(
        task_id=envelope.task_id,
        run_id=run_id,
        agent_id=agent.id,
        session_id=conversation.external_session_id,
        status=AgentRunStatus.COMPLETED,
        payload=ChiefChapterLaneSubmission(
            run_id=run_id,
            chapter_id="1",
            section_ids=["1.1", "1.2", "1.3"],
            part_refs={
                CHIEF_SECTION_RESULT_PART_IDS[section_id]: (
                    f"Work/runs/{run_id}/drafts/chief-chapter-1/r0/{section_id}.md"
                )
                for section_id in ("1.1", "1.2", "1.3")
            },
            revision=0,
        ),
    )
    try:
        attempt.activate()
        TaskAttemptStore(tmp_path, run_id).persist_result(
            attempt.correlation,
            persisted.model_dump(mode="json"),
            status="completed",
        )
    finally:
        attempt.close()

    loop_calls: list[object] = []

    def forbidden_loop(**kwargs: object) -> object:
        loop_calls.append(kwargs)
        raise AssertionError("persisted Chief result must not create a Provider loop")

    services = RuntimeServicesView(
        workspace=tmp_path,
        bus=bus,
        active_provider=object(),
        agent_defaults=AgentDefaults(),
        global_knowledge_root=None,
    )
    composition = build_chief_provider_composition(
        services,
        chief_runtime=chief_runtime,
        execution=AgentExecutionService(bus, timeout=1),
        loop_builder=forbidden_loop,
    )
    try:
        outcome = await composition.agent_invokers[agent.id].invoke(
            agent,
            task,
            context.model_dump(mode="json"),
            conversation,
            task_id="invoke-chief-provider-persisted",
        )
    finally:
        await composition.close()
        bus.shutdown()

    assert outcome.status == "ok"
    assert outcome.session_id == conversation.external_session_id
    assert outcome.result["submission"]["kind"] == "chief_chapter_lane_submission"
    assert loop_calls == []
    assert composition.execution.sessions == {}


def test_chief_provider_does_not_load_core_reporting() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys\n"
                "from manyselves.capabilities.distribution_reporting.runtime.chief_provider "
                "import build_chief_provider_composition\n"
                "print(sorted(name for name in sys.modules if name.startswith('manyselves.core.reporting')))"
            ),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert completed.stdout.strip() == "[]"

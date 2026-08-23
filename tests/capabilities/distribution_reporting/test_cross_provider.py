"""Characterization for the Capability-owned initial Cross Provider path."""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest

from manyselves.application.runtime_services import RuntimeServicesView
from manyselves.capabilities.distribution_reporting.domain.taxonomy import REPORT_TAXONOMY
from manyselves.capabilities.distribution_reporting.runtime.cross_owner_runtime import (
    CrossOwnerRuntime,
)
from manyselves.capabilities.distribution_reporting.runtime.cross_recheck import (
    prepare_cross_owner_recheck,
)
from manyselves.capabilities.distribution_reporting.runtime.models.agentic import (
    CrossOwnerFindingSubmission,
    CrossReviewCoverageEntry,
    CrossReviewFinding,
    ModuleReviewCoverage,
    ModuleReviewFindingSubmission,
    ModuleRevisionSubmission,
    ModuleSubmission,
    ResolutionVerdict,
    RevisionResponse,
)
from manyselves.capabilities.distribution_reporting.runtime.models.cross_owner import (
    DeclarativeCrossOwnerInitialAgentResult,
    DeclarativeCrossOwnerRecheckAgentResult,
)
from manyselves.capabilities.distribution_reporting.runtime.models.inputs import (
    ReviewCompletionRecord,
)
from manyselves.capabilities.distribution_reporting.runtime.models.module_lane import (
    DeclarativeModuleRevisionAgentResult,
)
from manyselves.config.schema import AgentDefaults
from manyselves.core.loops.bus import MessageBus
from manyselves.interfaces.types import UserMessage
from manyselves.kernel.conversations import (
    ConversationKey,
    ConversationMode,
    ConversationRecord,
)
from manyselves.kernel.definitions import AgentDefinition, TaskDefinition
from manyselves.runtime.agent_execution import AgentExecutionService
from tests.capabilities.distribution_reporting.test_cross_recheck import (
    _frozen_owner_input,
)
from tests.capabilities.distribution_reporting.test_cross_round import (
    _finding,
    _lane,
)


def _state(run_id: str) -> dict[str, object]:
    return {
        "run_id": run_id,
        "module_submissions": {
            module_id: ModuleSubmission(
                module_id=module_id,
                submodule_narratives={
                    submodule_id: f"内容 {submodule_id}"
                    for submodule_id in REPORT_TAXONOMY[module_id].submodules
                },
                claims=[],
                source_ids=[],
                unresolved_questions=[],
                revision=0,
            )
            for module_id in REPORT_TAXONOMY
        },
        "module_artifact_refs": {
            module_id: {
                "ref": f"Work/runs/{run_id}/modules/{module_id}-r0.json",
                "sha256": "0" * 64,
            }
            for module_id in REPORT_TAXONOMY
        },
    }


@pytest.mark.asyncio
async def test_cross_provider_uses_real_submit_tool_wire_identity_and_one_session(
    tmp_path: Path,
) -> None:
    from manyselves.capabilities.distribution_reporting.runtime.cross_provider import (
        build_cross_provider_composition,
    )

    run_id = "cross-provider-run"
    bus = MessageBus()
    bus_task = asyncio.create_task(bus.process_queue())
    built: list[dict[str, object]] = []
    loops: list[object] = []

    class Loop:
        def __init__(self, kwargs: dict[str, object]) -> None:
            self.kwargs = kwargs
            self.received: list[UserMessage] = []
            self.callback = None

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
                await self.kwargs["tools"].get("submit_result")(
                    kind="cross_owner_finding_submission",
                    owner_module_id="2.1",
                    coverage={
                        "module_id": "2.1",
                        "checked_dimensions": [
                            "terminology",
                            "facts",
                            "risk_levels",
                            "dependencies",
                            "propagation",
                            "joint_verification",
                        ],
                    },
                    findings=[],
                    synthesis_inputs=[],
                )

            self.callback = respond
            bus.subscribe(UserMessage, respond)

        async def stop(self) -> None:
            if self.callback is not None:
                bus.unsubscribe(UserMessage, self.callback)

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
    runtime = CrossOwnerRuntime(tmp_path)
    state = runtime.prepare(_state(run_id))
    composition = build_cross_provider_composition(
        services,
        cross_runtime=runtime,
        execution=AgentExecutionService(bus, timeout=1),
        loop_builder=loop_builder,
    )
    context = runtime.prepare_initial(
        {"state": state, "owner_module_id": "2.1"}
    )
    conversation = ConversationRecord(
        conversation_id="cross-provider-conversation",
        key=ConversationKey(
            agent_id="cross-module-reviewer",
            value="cross-owner-2.1",
            mode=ConversationMode.RUN,
        ),
        run_id=run_id,
    )
    agent = AgentDefinition(
        id="cross-module-reviewer",
        version="1.0.0",
        description="Cross owner reviewer",
        instructions="审查当前 owner 与其他模块的关系。",
        tools=["submit_result"],
    )
    task = TaskDefinition(
        id="cross-owner-runtime-initial-review",
        version="1.0.0",
        description="Cross owner initial",
        agent=agent.id,
        objective="review cross-module interfaces",
        input_contract="cross_owner_input",
        output_contract="declarative_cross_owner_initial_agent_result",
        tools=["submit_result"],
    )
    try:
        first = await composition.agent_invokers[agent.id].invoke(
            agent,
            task,
            context.model_dump(mode="json"),
            conversation,
            task_id="invoke-cross-owner-2.1",
        )
        second = await composition.agent_invokers[agent.id].invoke(
            agent,
            task,
            context.model_dump(mode="json"),
            conversation,
            task_id="invoke-cross-owner-2.1-again",
        )
    finally:
        await composition.close()
        bus.shutdown()
        await bus_task

    assert first.status == "ok"
    assert second.status == "ok"
    assert len(loops) == 1
    assert len(built) == 1
    assert [message.task_id for message in loops[0].received] == [
        "invoke-cross-owner-2.1",
        "invoke-cross-owner-2.1-again",
    ]
    assert [message.task_attempt_id for message in loops[0].received] == [
        "invoke-cross-owner-2.1",
        "invoke-cross-owner-2.1-again",
    ]
    assert [message.session_id for message in loops[0].received] == [
        conversation.external_session_id,
        conversation.external_session_id,
    ]
    assert conversation.external_session_id == "public-reporting:cross-owner-2.1"
    assert set(built[0]["tools"].get_all()) == {"submit_result"}

    result_path = (
        tmp_path
        / "Work/runs"
        / run_id
        / "results"
        / "cross-owner-2.1-r0-initial.json"
    )
    persisted = json.loads(result_path.read_text(encoding="utf-8"))
    assert persisted["agent_id"] == "cross-module-reviewer"
    assert persisted["task_id"] == "cross-owner-2.1-r0-initial"
    assert persisted["session_id"] == conversation.external_session_id
    assert persisted["payload"]["kind"] == "cross_owner_finding_submission"


def test_cross_provider_does_not_load_core_reporting() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys\n"
                "from manyselves.capabilities.distribution_reporting.runtime.cross_provider "
                "import build_cross_provider_composition\n"
                "print(sorted(name for name in sys.modules if name.startswith('manyselves.core.reporting')))"
            ),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert completed.stdout.strip() == "[]"


@pytest.mark.asyncio
async def test_cross_owner_finding_revises_locally_rechecks_and_completes(
    tmp_path: Path,
) -> None:
    """Characterize the full owner finding path before the Capability port exists."""

    run_id = "cross-owner-finding-lifecycle"
    runtime = CrossOwnerRuntime(tmp_path)
    raw_state = _state(run_id)
    for module_id, submission in raw_state["module_submissions"].items():
        runtime.store.write_json(
            f"Work/runs/{run_id}/modules/{module_id}-r0.json",
            submission.model_dump(mode="json"),
        )
    completion_ref = f"Work/runs/{run_id}/reviews/module/initial/2.1/completion-r0.json"
    runtime.store.write_json(
        completion_ref,
        ReviewCompletionRecord(
            lifecycle="module",
            run_id=run_id,
            reviewer_agent_id="evidence-auditor",
            reviewer_session_key="module-auditor-2.1",
            subject_refs=[f"Work/runs/{run_id}/modules/2.1-r0.json"],
            finding_refs=[],
            verdict_refs=[],
            resolved_finding_ids=[],
        ).model_dump(mode="json"),
    )
    raw_state["module_review_completion_refs"] = {"2.1": completion_ref}
    state = runtime.prepare(raw_state)
    owner_module_id = "2.1"
    finding = CrossReviewFinding(
        id="X-2.1-r0-1",
        owner_module_id=owner_module_id,
        target_submodule_ids=["2.1.1"],
        related_module_ids=["2.2"],
        category="dependencies",
        impact="blocking",
        observation="当前模块没有把该关系写入可验证的模块正文，导致关联模块无法复核其依赖边界。",
        evidence_refs=["Work/runs/cross-owner-finding-lifecycle/modules/2.1-r0.json"],
        required_change="在 2.1.1 中补充关系机制、影响路径和与 2.2 的联合验证步骤。",
        reviewer_checks=["确认关系机制已写入目标小节并能由关联模块复核。"],
    )
    initial_result = CrossOwnerFindingSubmission(
        owner_module_id=owner_module_id,
        coverage=CrossReviewCoverageEntry(
            module_id=owner_module_id,
            checked_dimensions=[
                "terminology",
                "facts",
                "risk_levels",
                "dependencies",
                "propagation",
                "joint_verification",
            ],
        ),
        findings=[finding],
    )
    initial_context = runtime.prepare_initial(
        {"state": state, "owner_module_id": owner_module_id}
    )
    assert initial_context.preparation is not None
    assert (
        initial_context.preparation.prior_module_review_completion_ref
        == completion_ref
    )
    context = runtime.accept_initial(
        {
            "context": initial_context,
            "result": DeclarativeCrossOwnerInitialAgentResult(
                status="completed",
                submission=initial_result,
            ),
        }
    )
    assert context.status == "initial_accepted"
    assert context.acceptance is not None
    assert context.acceptance.prior_module_review_completion_ref == completion_ref

    revision_context = await runtime.prepare_revision(context)
    assert revision_context.status == "revision_ready"
    assert revision_context.revision_preparation is not None
    prepared = revision_context.revision_preparation.prepared
    assert prepared is not None
    revised = ModuleRevisionSubmission(
        module_id=owner_module_id,
        base_revision=0,
        revision=1,
        submodule_narratives={"2.1.1": "补充依赖关系机制与联合验证。"},
        source_ids=[],
        revision_responses=[
            RevisionResponse(
                finding_id=finding.id,
                action="implemented",
                summary="已补充该关系机制及其联合验证步骤，供后续复核。",
                changed_target_ids=["2.1.1"],
            )
        ],
    )
    revised_context = runtime.accept_revision(
        {
            "context": revision_context,
            "result": DeclarativeModuleRevisionAgentResult(
                status="completed",
                submission=revised,
            ),
        }
    )
    assert revised_context.status == "revision_accepted"
    author_exception_context = runtime.prepare_author_exception(revised_context)
    assert author_exception_context.status == "author_exception_not_required"

    local_context = await runtime.prepare_local_review(author_exception_context)
    assert local_context.status == "local_review_ready"
    local_review = runtime.accept_local_review(
        {
            "context": local_context,
            "result": ModuleReviewFindingSubmission(
                coverage=ModuleReviewCoverage(submodule_ids=["2.1.1"]),
                findings=[],
            ),
        }
    )
    assert local_review.status == "local_review_accepted"
    recheck_context = await runtime.prepare_recheck(local_review)
    assert recheck_context.status == "recheck_ready"
    verdict_context = runtime.accept_recheck(
        {
            "context": recheck_context,
            "result": {
                "status": "completed",
                "submission": {
                    "owner_module_id": owner_module_id,
                    "coverage": {
                        "module_id": owner_module_id,
                        "checked_dimensions": [
                            "terminology",
                            "facts",
                            "risk_levels",
                            "dependencies",
                            "propagation",
                            "joint_verification",
                        ],
                    },
                    "verdicts": [
                        ResolutionVerdict(
                            finding_id=finding.id,
                            verdict="resolved",
                            reason="修订后的关系机制和联合验证已经满足原 finding 的检查要求。",
                            evidence_refs=[
                                f"Work/runs/{run_id}/modules/2.1-r1.json"
                            ],
                        )
                    ],
                },
            },
        }
    )
    assert verdict_context.status == "recheck_accepted"
    reviewer_exception_context = runtime.prepare_reviewer_exception(verdict_context)
    assert reviewer_exception_context.status == "reviewer_exception_not_required"
    completed_context = await runtime.advance_round(reviewer_exception_context)
    assert completed_context.status == "round_completed"
    outcome = await runtime.complete_owner_round(completed_context)
    assert outcome.status == "completed"


@pytest.mark.asyncio
async def test_cross_provider_recheck_rebinds_tools_and_terminal_on_same_session(
    tmp_path: Path,
) -> None:
    """Characterize the same-session initial -> recheck Provider boundary."""

    from manyselves.capabilities.distribution_reporting.runtime.cross_provider import (
        build_cross_provider_composition,
    )

    run_id = "cross-round-characterization"
    bus = MessageBus()
    bus_task = asyncio.create_task(bus.process_queue())
    built: list[dict[str, object]] = []
    loops: list[object] = []

    finding = _finding("F-1")
    lane = _lane()

    class Loop:
        def __init__(self, kwargs: dict[str, object]) -> None:
            self.kwargs = kwargs
            self.agent_type = kwargs["agent_type"]
            self.tools = kwargs["tools"]
            self.received: list[UserMessage] = []
            self.tool_snapshots: list[object] = []
            self.contents: list[str] = []
            self.terminal_task_ids: list[str] = []
            self.callback = None

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
                if message.agent_type != self.agent_type:
                    return
                self.received.append(message)
                self.contents.append(message.content)
                registry = self.tools
                submit_result = registry.get("submit_result")
                self.tool_snapshots.append(registry)
                self.terminal_task_ids.append(submit_result.task_id)
                if len(self.received) == 1:
                    await submit_result(
                        kind="cross_owner_finding_submission",
                        owner_module_id="2.1",
                        coverage={
                            "module_id": "2.1",
                            "checked_dimensions": [
                                "terminology",
                                "facts",
                                "risk_levels",
                                "dependencies",
                                "propagation",
                                "joint_verification",
                            ],
                        },
                        findings=[finding.model_dump(mode="json")],
                        synthesis_inputs=[],
                    )
                else:
                    await submit_result(
                        kind="cross_owner_verdict_submission",
                        owner_module_id="2.1",
                        coverage={
                            "module_id": "2.1",
                            "checked_dimensions": [
                                "terminology",
                                "facts",
                                "risk_levels",
                                "dependencies",
                                "propagation",
                                "joint_verification",
                            ],
                        },
                        verdicts=[
                            {
                                "finding_id": finding.id,
                                "verdict": "resolved",
                                "reason": "当前修订已经满足原 finding 的全部检查要求。",
                                "evidence_refs": [
                                    f"Work/runs/{run_id}/modules/2.1-r0.json"
                                ],
                            }
                        ],
                        new_findings=[],
                    )

            self.callback = respond
            bus.subscribe(UserMessage, respond)

        async def stop(self) -> None:
            if self.callback is not None:
                bus.unsubscribe(UserMessage, self.callback)

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
    runtime = CrossOwnerRuntime(tmp_path)
    state = runtime.prepare(_state(run_id))
    initial_context = runtime.prepare_initial(
        {"state": state, "owner_module_id": "2.1"}
    )
    assert initial_context.preparation is not None
    assert (tmp_path / initial_context.preparation.owner_input_ref).is_file()

    composition = build_cross_provider_composition(
        services,
        cross_runtime=runtime,
        execution=AgentExecutionService(bus, timeout=1),
        loop_builder=loop_builder,
    )
    conversation = ConversationRecord(
        conversation_id="cross-provider-recheck-conversation",
        key=ConversationKey(
            agent_id="cross-module-reviewer",
            value="cross-owner-2.1",
            mode=ConversationMode.RUN,
        ),
        run_id=run_id,
    )
    agent = AgentDefinition(
        id="cross-module-reviewer",
        version="1.0.0",
        description="Cross owner reviewer",
        instructions="审查当前 owner 与其他模块的关系。",
        tools=["submit_result"],
    )
    initial_task = TaskDefinition(
        id="cross-owner-runtime-initial-review",
        version="1.0.0",
        description="Cross owner initial",
        agent=agent.id,
        objective="review cross-module interfaces",
        input_contract="cross_owner_input",
        output_contract="declarative_cross_owner_initial_agent_result",
        tools=["submit_result"],
    )
    recheck_task = TaskDefinition(
        id="cross-owner-runtime-recheck",
        version="1.0.0",
        description="Cross owner recheck",
        agent=agent.id,
        objective="recheck cross-module interfaces",
        input_contract="cross_owner_input",
        output_contract="declarative_cross_owner_recheck_agent_result",
        tools=["submit_result"],
    )
    try:
        first = await composition.agent_invokers[agent.id].invoke(
            agent,
            initial_task,
            initial_context.model_dump(mode="json"),
            conversation,
            task_id="invoke-cross-owner-2.1-initial",
        )
        assert first.status == "ok"
        accepted_context = runtime.accept_initial(
            {"context": initial_context, "result": first.result}
        )
        assert accepted_context.acceptance is not None

        frozen_input = _frozen_owner_input(finding, lane=lane)
        recheck_input_ref = (
            f"Work/runs/{run_id}/reviews/cross-owner-input-r1-2.1.json"
        )
        runtime.store.write_json(
            recheck_input_ref,
            frozen_input.model_dump(mode="json"),
        )
        recheck_preparation = prepare_cross_owner_recheck(
            workflow_id=accepted_context.acceptance.workflow_id,
            initial=accepted_context.acceptance,
            lane=lane,
            frozen_owner_input=frozen_input,
            owner_input_ref=recheck_input_ref,
            review_round=1,
            required_findings=list(frozen_input.required_findings),
            read_result=lambda _ref: None,
        )
        recheck_context = accepted_context.model_copy(
            update={
                "status": "recheck_ready",
                "recheck_preparation": recheck_preparation,
                "recheck_acceptance": None,
            }
        )
        second = await composition.agent_invokers[agent.id].invoke(
            agent,
            recheck_task,
            recheck_context.model_dump(mode="json"),
            conversation,
            task_id="invoke-cross-owner-2.1-recheck",
        )
    finally:
        await composition.close()
        bus.shutdown()
        await bus_task

    assert second.status == "ok"
    typed = DeclarativeCrossOwnerRecheckAgentResult.model_validate(second.result)
    assert typed.status == "completed"
    assert typed.submission is not None
    assert typed.submission.verdicts[0].finding_id == finding.id
    assert len(loops) == 1
    assert len(built) == 1
    assert len(loops[0].received) == 2
    assert loops[0].received[0].session_id == loops[0].received[1].session_id
    assert '"phase": "recheck"' in loops[0].contents[1]
    assert loops[0].terminal_task_ids == [
        "cross-owner-2.1-r0-initial",
        "cross-owner-2.1-r1-recheck",
    ]
    assert loops[0].tool_snapshots[0] is not loops[0].tool_snapshots[1]
    first_submit = loops[0].tool_snapshots[0].get("submit_result")
    second_submit = loops[0].tool_snapshots[1].get("submit_result")
    assert first_submit.task_id == "cross-owner-2.1-r0-initial"
    assert first_submit.allowed_outputs == frozenset(
        {"cross_owner_finding_submission"}
    )
    assert second_submit.task_id == "cross-owner-2.1-r1-recheck"
    assert second_submit.allowed_outputs == frozenset(
        {"cross_owner_verdict_submission"}
    )
    assert second_submit.input_contract_ref == recheck_input_ref
    result_path = (
        tmp_path
        / "Work/runs"
        / run_id
        / "results"
        / "cross-owner-2.1-r1-recheck.json"
    )
    persisted = json.loads(result_path.read_text(encoding="utf-8"))
    assert persisted["task_id"] == "cross-owner-2.1-r1-recheck"
    assert persisted["payload"]["kind"] == "cross_owner_verdict_submission"

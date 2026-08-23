"""Stable semantic trace for the current Legacy module review lane."""

from pathlib import Path

import pytest

from manyselves.capabilities.distribution_reporting.domain.taxonomy import REPORT_TAXONOMY
from manyselves.capabilities.distribution_reporting.runtime.models.agentic import (
    ModuleReviewFindingSubmission,
    ModuleReviewVerdictSubmission,
    ModuleRevisionSubmission,
)
from manyselves.core.reporting.review_lifecycle import run_module_review
from manyselves.runtime.semantic_trace import SemanticEventKind, SemanticTraceRecorder
from tests.reporting.test_agent_workflow import _module, _ScriptedRunner

_BRANCH_BY_OUTPUT = {
    "module_review_finding_submission": "revision_required",
    "module_revision_submission": "reviewer_recheck",
    "module_review_verdict_submission": "lane_completed",
}


class _LegacyModuleLaneTraceAdapter(_ScriptedRunner):
    """Observe the scripted Legacy runner without changing workflow behavior."""

    def __init__(self, workspace: Path, scripted: list[tuple[str, str, object]]):
        super().__init__(workspace, scripted)
        self.trace = SemanticTraceRecorder()
        self._seen_conversations: set[str] = set()

    async def _agent(
        self,
        agent_id,
        envelope,
        artifacts,
        workflow_id,
        *,
        session_key=None,
    ):
        conversation_key = session_key or agent_id
        output_contract = envelope.allowed_outputs[0]
        self.trace.record(
            SemanticEventKind.ACTION_STARTED,
            workflow_id=workflow_id,
            action_id=envelope.task_id,
        )
        if conversation_key not in self._seen_conversations:
            self.trace.record(
                SemanticEventKind.CONVERSATION_CREATED,
                workflow_id=workflow_id,
                action_id=envelope.task_id,
                agent_id=agent_id,
                conversation_key=conversation_key,
            )
            self._seen_conversations.add(conversation_key)
        self.trace.record(
            SemanticEventKind.AGENT_INVOKED,
            workflow_id=workflow_id,
            action_id=envelope.task_id,
            agent_id=agent_id,
            conversation_key=conversation_key,
            input_contract=envelope.input_contract_kind,
            output_contract=output_contract,
        )
        try:
            result = await super()._agent(
                agent_id,
                envelope,
                artifacts,
                workflow_id,
                session_key=session_key,
            )
        except BaseException:
            self.trace.record(
                SemanticEventKind.ACTION_FAILED,
                workflow_id=workflow_id,
                action_id=envelope.task_id,
                status="failed",
            )
            raise
        self.trace.record(
            SemanticEventKind.CONTRACT_VALIDATED,
            workflow_id=workflow_id,
            action_id=envelope.task_id,
            output_contract=output_contract,
            status="passed",
        )
        self.trace.record(
            SemanticEventKind.BRANCH_SELECTED,
            workflow_id=workflow_id,
            action_id=envelope.task_id,
            branch=_BRANCH_BY_OUTPUT[output_contract],
        )
        self.trace.record(
            SemanticEventKind.ACTION_COMPLETED,
            workflow_id=workflow_id,
            action_id=envelope.task_id,
            status="completed",
        )
        return result


def _module_lane_script(
    run_id: str = "run-wp00-module-lane",
) -> tuple[object, str, list[tuple[str, str, object]]]:
    module = _module("2.1")
    target = next(iter(REPORT_TAXONOMY["2.1"].submodules))
    finding_id = "M-2.1-initial-r0-001"
    finding = {
        "id": finding_id,
        "target_submodule_id": target,
        "category": "analysis_depth",
        "impact": "advisory",
        "observation": "当前建议缺少责任接口和可复核的验收方式。",
        "evidence_refs": [f"Work/runs/{run_id}/modules/2.1-r0.json"],
        "required_change": "补充责任接口、执行动作和可复核验收方法。",
        "reviewer_checks": ["责任、动作和验收形成闭环"],
    }
    revision = ModuleRevisionSubmission(
        module_id="2.1",
        base_revision=0,
        revision=1,
        submodule_narratives={target: "### 修订\n\n已补充责任、动作和验收方法。"},
        claims_upsert=[],
        claim_ids_remove=[],
        source_ids=[],
        unresolved_questions=[],
        revision_responses=[
            {
                "finding_id": finding_id,
                "action": "implemented",
                "summary": "已在目标小节补充责任接口、执行动作和可复核的验收方法。",
                "changed_target_ids": [target],
            }
        ],
    )
    return (
        module,
        target,
        [
            (
                "evidence-auditor",
                "module_review_finding_submission",
                ModuleReviewFindingSubmission(
                    coverage={"submodule_ids": [target]},
                    findings=[finding],
                ),
            ),
            ("module-2.1-specialist", "module_revision_submission", revision),
            (
                "evidence-auditor",
                "module_review_verdict_submission",
                ModuleReviewVerdictSubmission(
                    coverage={"submodule_ids": [target]},
                    verdicts=[
                            {
                                "finding_id": finding_id,
                                "verdict": "resolved",
                                "reason": "当前修订已补充责任接口、执行动作和可复核验收方法，可以关闭。",
                            "evidence_refs": [
                                f"Work/runs/{run_id}/modules/2.1-r1.json"
                            ],
                        }
                    ],
                    new_findings=[],
                ),
            ),
        ],
    )


async def _capture_legacy_module_lane(workspace: Path) -> list[dict[str, str]]:
    workflow_id = "workflow-wp00-module-lane"
    run_id = "run-wp00-module-lane"
    module, target, scripted = _module_lane_script(run_id)
    runner = _LegacyModuleLaneTraceAdapter(workspace, scripted)
    runner.trace.record(
        SemanticEventKind.WORKFLOW_STARTED,
        workflow_id=workflow_id,
        status="running",
    )

    result = await run_module_review(
        runner,
        "2.1",
        module,
        {"run_id": run_id},
        workflow_id,
        initial_scope={target},
        lifecycle_id="initial",
    )

    runner.trace.record(
        SemanticEventKind.OUTPUT_PUBLISHED,
        workflow_id=workflow_id,
        action_id="module-2.1-review-lane",
        output_contract="module_submission",
        output_id=f"module-2.1-r{result.revision}",
        status="completed",
    )
    runner.trace.record(
        SemanticEventKind.WORKFLOW_COMPLETED,
        workflow_id=workflow_id,
        status="completed",
    )
    return runner.trace.snapshot()


@pytest.mark.asyncio
async def test_legacy_module_lane_semantic_trace_is_repeatable(tmp_path: Path) -> None:
    first = await _capture_legacy_module_lane(tmp_path / "first")
    second = await _capture_legacy_module_lane(tmp_path / "second")

    assert first == second
    assert first == [
        {
            "event": "workflow.started",
            "workflow_id": "workflow-wp00-module-lane",
            "status": "running",
        },
        {
            "event": "action.started",
            "workflow_id": "workflow-wp00-module-lane",
            "action_id": "module-2.1-initial-review-r0",
        },
        {
            "event": "conversation.created",
            "workflow_id": "workflow-wp00-module-lane",
            "action_id": "module-2.1-initial-review-r0",
            "agent_id": "evidence-auditor",
            "conversation_key": "module-auditor-2.1",
        },
        {
            "event": "agent.invoked",
            "workflow_id": "workflow-wp00-module-lane",
            "action_id": "module-2.1-initial-review-r0",
            "agent_id": "evidence-auditor",
            "conversation_key": "module-auditor-2.1",
            "input_contract": "module_review_input",
            "output_contract": "module_review_finding_submission",
        },
        {
            "event": "contract.validated",
            "workflow_id": "workflow-wp00-module-lane",
            "action_id": "module-2.1-initial-review-r0",
            "output_contract": "module_review_finding_submission",
            "status": "passed",
        },
        {
            "event": "branch.selected",
            "workflow_id": "workflow-wp00-module-lane",
            "action_id": "module-2.1-initial-review-r0",
            "branch": "revision_required",
        },
        {
            "event": "action.completed",
            "workflow_id": "workflow-wp00-module-lane",
            "action_id": "module-2.1-initial-review-r0",
            "status": "completed",
        },
        {
            "event": "action.started",
            "workflow_id": "workflow-wp00-module-lane",
            "action_id": "module-revision-r1-2.1",
        },
        {
            "event": "conversation.created",
            "workflow_id": "workflow-wp00-module-lane",
            "action_id": "module-revision-r1-2.1",
            "agent_id": "module-2.1-specialist",
            "conversation_key": "module-2.1",
        },
        {
            "event": "agent.invoked",
            "workflow_id": "workflow-wp00-module-lane",
            "action_id": "module-revision-r1-2.1",
            "agent_id": "module-2.1-specialist",
            "conversation_key": "module-2.1",
            "input_contract": "module_revision_input",
            "output_contract": "module_revision_submission",
        },
        {
            "event": "contract.validated",
            "workflow_id": "workflow-wp00-module-lane",
            "action_id": "module-revision-r1-2.1",
            "output_contract": "module_revision_submission",
            "status": "passed",
        },
        {
            "event": "branch.selected",
            "workflow_id": "workflow-wp00-module-lane",
            "action_id": "module-revision-r1-2.1",
            "branch": "reviewer_recheck",
        },
        {
            "event": "action.completed",
            "workflow_id": "workflow-wp00-module-lane",
            "action_id": "module-revision-r1-2.1",
            "status": "completed",
        },
        {
            "event": "action.started",
            "workflow_id": "workflow-wp00-module-lane",
            "action_id": "module-2.1-initial-review-r1",
        },
        {
            "event": "agent.invoked",
            "workflow_id": "workflow-wp00-module-lane",
            "action_id": "module-2.1-initial-review-r1",
            "agent_id": "evidence-auditor",
            "conversation_key": "module-auditor-2.1",
            "input_contract": "module_review_input",
            "output_contract": "module_review_verdict_submission",
        },
        {
            "event": "contract.validated",
            "workflow_id": "workflow-wp00-module-lane",
            "action_id": "module-2.1-initial-review-r1",
            "output_contract": "module_review_verdict_submission",
            "status": "passed",
        },
        {
            "event": "branch.selected",
            "workflow_id": "workflow-wp00-module-lane",
            "action_id": "module-2.1-initial-review-r1",
            "branch": "lane_completed",
        },
        {
            "event": "action.completed",
            "workflow_id": "workflow-wp00-module-lane",
            "action_id": "module-2.1-initial-review-r1",
            "status": "completed",
        },
        {
            "event": "output.published",
            "workflow_id": "workflow-wp00-module-lane",
            "action_id": "module-2.1-review-lane",
            "output_contract": "module_submission",
            "status": "completed",
            "output_id": "module-2.1-r1",
        },
        {
            "event": "workflow.completed",
            "workflow_id": "workflow-wp00-module-lane",
            "status": "completed",
        },
    ]
    auditor_conversation_events = [
        event
        for event in first
        if event["event"] == "conversation.created"
        and event.get("conversation_key") == "module-auditor-2.1"
    ]
    assert len(auditor_conversation_events) == 1

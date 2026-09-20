"""Characterization for the Capability-owned Cross round transition."""

from __future__ import annotations

import pytest

from manyselves.capabilities.distribution_reporting.domain.taxonomy import (
    REPORT_TAXONOMY,
)
from manyselves.capabilities.distribution_reporting.runtime.cross_round import (
    advance_cross_owner_round,
)
from manyselves.capabilities.distribution_reporting.runtime.models.agentic import (
    CrossOwnerFindingSubmission,
    CrossOwnerVerdictSubmission,
    CrossReviewCoverageEntry,
    CrossReviewFinding,
    ModuleSubmission,
    ResolutionVerdict,
    RevisionResponse,
    WorkflowDecisionSubmission,
)
from manyselves.capabilities.distribution_reporting.runtime.models.review import (
    CrossOwnerInitialReviewAcceptance,
    CrossOwnerRecheckAcceptance,
    MainExceptionDecisionAcceptance,
    _CrossOwnerLaneResult,
)
from manyselves.capabilities.distribution_reporting.runtime.state.parallel import (
    ArtifactRef,
    CrossOwnerCompletion,
)

RUN_ID = "cross-round-characterization"
OWNER_ID = "2.1"
DIMENSIONS = [
    "terminology",
    "facts",
    "risk_levels",
    "dependencies",
    "propagation",
    "joint_verification",
]


def _finding(finding_id: str) -> CrossReviewFinding:
    return CrossReviewFinding(
        id=finding_id,
        owner_module_id=OWNER_ID,
        target_submodule_ids=["2.1.1"],
        related_module_ids=["2.2"],
        category="dependencies",
        impact="blocking",
        observation="当前模块没有写出可验证的跨模块依赖关系、影响路径和联合验证边界。",
        evidence_refs=["Work/runs/cross-round-characterization/modules/2.1-r0.json"],
        required_change="补充依赖机制、影响路径以及与关联模块可以复核的联合验证步骤。",
        reviewer_checks=["确认依赖机制和联合验证已经写入目标小节。"],
    )


def _coverage() -> CrossReviewCoverageEntry:
    return CrossReviewCoverageEntry(
        module_id=OWNER_ID,
        checked_dimensions=DIMENSIONS,
    )


def _lane() -> _CrossOwnerLaneResult:
    module = ModuleSubmission(
        module_id=OWNER_ID,
        submodule_narratives={
            submodule_id: f"当前模块内容 {submodule_id}。"
            for submodule_id in REPORT_TAXONOMY[OWNER_ID].submodules
        },
        claims=[],
        source_ids=[],
        unresolved_questions=[],
        revision=0,
    )
    def artifact(ref: str) -> ArtifactRef:
        return ArtifactRef(ref=ref, sha256="0" * 64, size=0, media_type="application/json")

    completion = CrossOwnerCompletion(
        lane_id="cross-r1-module-2.1",
        run_id=RUN_ID,
        review_round=1,
        module_id=OWNER_ID,
        subject_revision=module.revision,
        owner_input=artifact("Work/runs/cross-round-characterization/reviews/input.json"),
        subject=artifact("Work/runs/cross-round-characterization/modules/2.1-r0.json"),
        local_review_completion=artifact(
            "Work/runs/cross-round-characterization/reviews/local.json"
        ),
    )
    return _CrossOwnerLaneResult(
        module=module,
        responses=[
            RevisionResponse(
                finding_id="F-1",
                action="implemented",
                summary="作者已经按 finding 补充了依赖机制和联合验证步骤。",
                changed_target_ids=["2.1.1"],
            )
        ],
        local_review_ref="Work/runs/cross-round-characterization/reviews/local.json",
        completion_ref=(
            "Work/runs/cross-round-characterization/lanes/cross-r1/"
            "module-2.1/completion-r0.json"
        ),
        completion=completion,
    )


def _acceptance(
    finding: CrossReviewFinding,
    verdict: ResolutionVerdict,
    *,
    new_findings: list[CrossReviewFinding] | None = None,
    result_ref: str = (
        "Work/runs/cross-round-characterization/reviews/"
        "cross-owner-verdicts-r1-2.1.json"
    ),
) -> tuple[CrossOwnerInitialReviewAcceptance, CrossOwnerRecheckAcceptance]:
    initial_result = CrossOwnerFindingSubmission(
        owner_module_id=OWNER_ID,
        coverage=_coverage(),
        findings=[finding],
        synthesis_inputs=[],
    )
    initial = CrossOwnerInitialReviewAcceptance(
        run_id=RUN_ID,
        workflow_id="distribution-cross-owner-2.1-pipeline",
        owner_module_id=OWNER_ID,
        reviewer_session_key="cross-owner-2.1",
        owner_input_ref="Work/runs/cross-round-characterization/reviews/input.json",
        result_ref=(
            "Work/runs/cross-round-characterization/reviews/"
            "cross-owner-findings-r0-2.1.json"
        ),
        result=initial_result,
    )
    recheck_result = CrossOwnerVerdictSubmission(
        owner_module_id=OWNER_ID,
        coverage=_coverage(),
        verdicts=[verdict],
        new_findings=new_findings or [],
    )
    acceptance = CrossOwnerRecheckAcceptance(
        run_id=RUN_ID,
        workflow_id=initial.workflow_id,
        owner_module_id=OWNER_ID,
        review_round=1,
        reviewer_session_key=initial.reviewer_session_key,
        initial_result_ref=initial.result_ref,
        initial_result=initial.result,
        lane=_lane(),
        owner_input_ref="Work/runs/cross-round-characterization/reviews/recheck-input.json",
        required_findings=[finding],
        result_ref=result_ref,
        result=recheck_result,
    )
    return initial, acceptance


def _main_acceptance(finding_id: str) -> MainExceptionDecisionAcceptance:
    return MainExceptionDecisionAcceptance(
        run_id=RUN_ID,
        workflow_id="distribution-cross-owner-2.1-pipeline",
        scope="cross",
        trigger="reviewer_escalation",
        decision_ref="Work/runs/cross-round-characterization/exceptions/decision.json",
        result=WorkflowDecisionSubmission(
            decision="accept_dispute",
            rationale="Main 已核对当前证据并接受作者对该 finding 的争议处理。",
            finding_ids=[finding_id],
        ),
    )


@pytest.mark.asyncio
async def test_advance_cross_round_accumulates_resolved_and_refs() -> None:
    finding = _finding("F-1")
    initial, acceptance = _acceptance(
        finding,
        ResolutionVerdict(
            finding_id=finding.id,
            verdict="resolved",
            reason="当前修订已经满足原 finding 的全部检查要求。",
            evidence_refs=["Work/runs/cross-round-characterization/modules/2.1-r0.json"],
        ),
    )

    progress = await advance_cross_owner_round(
        workflow_id=initial.workflow_id,
        initial_input_ref=initial.owner_input_ref,
        initial_result_ref=initial.result_ref,
        initial_result=initial.result,
        acceptance=acceptance,
    )

    assert progress.next_action == "completed"
    assert progress.pending == []
    assert progress.resolved_ids == [finding.id]
    assert progress.finding_refs == [initial.result_ref]
    assert progress.verdict_refs == [acceptance.result_ref]
    assert progress.next_review_round == 2
    assert progress.next_owner_input_ref == acceptance.result_ref


@pytest.mark.asyncio
async def test_advance_cross_round_keeps_open_finding_pending() -> None:
    finding = _finding("F-1")
    initial, acceptance = _acceptance(
        finding,
        ResolutionVerdict(
            finding_id=finding.id,
            verdict="open",
            reason="修订仍然缺少能够支撑联合验证的具体关系证据。",
        ),
    )

    progress = await advance_cross_owner_round(
        workflow_id=initial.workflow_id,
        initial_input_ref=initial.owner_input_ref,
        initial_result_ref=initial.result_ref,
        initial_result=initial.result,
        acceptance=acceptance,
    )

    assert progress.next_action == "revise"
    assert [item.id for item in progress.pending] == [finding.id]
    assert progress.resolved_ids == []
    assert progress.finding_refs == [initial.result_ref]
    assert progress.verdict_refs == [acceptance.result_ref]


@pytest.mark.asyncio
async def test_advance_cross_round_accept_dispute_resolves_escalation() -> None:
    finding = _finding("F-1")
    initial, acceptance = _acceptance(
        finding,
        ResolutionVerdict(
            finding_id=finding.id,
            verdict="escalate",
            reason="作者与 reviewer 对当前关系证据的解释存在明确分歧。",
        ),
    )

    progress = await advance_cross_owner_round(
        workflow_id=initial.workflow_id,
        initial_input_ref=initial.owner_input_ref,
        initial_result_ref=initial.result_ref,
        initial_result=initial.result,
        acceptance=acceptance,
        main_decision=_main_acceptance(finding.id),
    )

    assert progress.next_action == "completed"
    assert progress.pending == []
    assert progress.resolved_ids == [finding.id]


@pytest.mark.asyncio
async def test_advance_cross_round_adds_new_finding_and_accumulates_refs() -> None:
    finding = _finding("F-1")
    new_finding = _finding("F-2")
    initial, acceptance = _acceptance(
        finding,
        ResolutionVerdict(
            finding_id=finding.id,
            verdict="resolved",
            reason="原 finding 已经完成，当前回归又发现一个新的关系缺口。",
            evidence_refs=["Work/runs/cross-round-characterization/modules/2.1-r0.json"],
        ),
        new_findings=[new_finding],
    )
    written: list[str] = []

    def write_immutable(ref: str, _value: object) -> str:
        written.append(ref)
        return ref

    progress = await advance_cross_owner_round(
        workflow_id=initial.workflow_id,
        initial_input_ref=initial.owner_input_ref,
        initial_result_ref=initial.result_ref,
        initial_result=initial.result,
        acceptance=acceptance,
        write_immutable=write_immutable,
    )

    regression_ref = (
        "Work/runs/cross-round-characterization/reviews/"
        "cross-owner-regression-findings-r1-2.1.json"
    )
    assert progress.next_action == "revise"
    assert [item.id for item in progress.pending] == [new_finding.id]
    assert progress.resolved_ids == [finding.id]
    assert progress.finding_refs == [initial.result_ref, regression_ref]
    assert progress.verdict_refs == [acceptance.result_ref]
    assert progress.findings[-1].id == new_finding.id
    assert written == [regression_ref]


@pytest.mark.asyncio
async def test_advance_cross_round_accumulates_previous_progress_refs() -> None:
    finding = _finding("F-1")
    initial, first_acceptance = _acceptance(
        finding,
        ResolutionVerdict(
            finding_id=finding.id,
            verdict="open",
            reason="第一轮修订仍然需要补充关系证据和联合验证范围。",
        ),
        result_ref=(
            "Work/runs/cross-round-characterization/reviews/"
            "cross-owner-verdicts-r1-2.1.json"
        ),
    )
    first_progress = await advance_cross_owner_round(
        workflow_id=initial.workflow_id,
        initial_input_ref=initial.owner_input_ref,
        initial_result_ref=initial.result_ref,
        initial_result=initial.result,
        acceptance=first_acceptance,
    )

    _, second_acceptance = _acceptance(
        finding,
        ResolutionVerdict(
            finding_id=finding.id,
            verdict="resolved",
            reason="第二轮修订已经补齐关系证据并满足 reviewer 检查要求。",
            evidence_refs=["Work/runs/cross-round-characterization/modules/2.1-r0.json"],
        ),
        result_ref=(
            "Work/runs/cross-round-characterization/reviews/"
            "cross-owner-verdicts-r2-2.1.json"
        ),
    )
    second_progress = await advance_cross_owner_round(
        workflow_id=initial.workflow_id,
        initial_input_ref=initial.owner_input_ref,
        initial_result_ref=initial.result_ref,
        initial_result=initial.result,
        acceptance=second_acceptance,
        previous=first_progress,
    )

    assert second_progress.next_action == "completed"
    assert second_progress.pending == []
    assert second_progress.resolved_ids == [finding.id]
    assert second_progress.finding_refs == [initial.result_ref]
    assert second_progress.verdict_refs == [
        first_acceptance.result_ref,
        second_acceptance.result_ref,
    ]
    assert [item.finding_id for item in second_progress.verdicts] == [finding.id]


@pytest.mark.asyncio
async def test_advance_cross_round_rejects_duplicate_new_finding_id() -> None:
    finding = _finding("F-1")
    initial, acceptance = _acceptance(
        finding,
        ResolutionVerdict(
            finding_id=finding.id,
            verdict="resolved",
            reason="原 finding 已经完成，但提交错误地复用了同一个 finding id。",
            evidence_refs=["Work/runs/cross-round-characterization/modules/2.1-r0.json"],
        ),
        new_findings=[_finding(finding.id)],
    )

    with pytest.raises(ValueError, match="existing id"):
        await advance_cross_owner_round(
            workflow_id=initial.workflow_id,
            initial_input_ref=initial.owner_input_ref,
            initial_result_ref=initial.result_ref,
            initial_result=initial.result,
            acceptance=acceptance,
        )

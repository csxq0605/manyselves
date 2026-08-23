"""Characterization for reducing completed Cross owner pipelines."""

from __future__ import annotations

from pathlib import Path

from manyselves.capabilities.distribution_reporting.domain.taxonomy import (
    REPORT_TAXONOMY,
)
from manyselves.capabilities.distribution_reporting.runtime.cross_completion import (
    build_cross_owner_lane_completion,
)
from manyselves.capabilities.distribution_reporting.runtime.cross_owner_runtime import (
    CrossOwnerRuntime,
)
from manyselves.capabilities.distribution_reporting.runtime.models.agentic import (
    CROSS_REVIEW_DIMENSIONS,
    CrossOwnerFindingSubmission,
    CrossOwnerVerdictSubmission,
    CrossReviewCoverageEntry,
    CrossReviewFinding,
    ModuleSubmission,
    ResolutionVerdict,
    RevisionResponse,
)
from manyselves.capabilities.distribution_reporting.runtime.models.cross_owner import (
    DeclarativeCrossOwnerPipelineOutcome,
)
from manyselves.capabilities.distribution_reporting.runtime.models.inputs import (
    ReviewCompletionRecord,
)
from manyselves.capabilities.distribution_reporting.runtime.models.review import (
    _CrossOwnerPipelineResult,
)
from manyselves.capabilities.distribution_reporting.runtime.storage import ReportingStore


def _module(module_id: str, *, revision: int = 0) -> ModuleSubmission:
    return ModuleSubmission(
        module_id=module_id,
        submodule_narratives={
            submodule_id: f"内容 {submodule_id}"
            for submodule_id in REPORT_TAXONOMY[module_id].submodules
        },
        claims=[],
        source_ids=[],
        unresolved_questions=[],
        revision=revision,
        revision_responses=(
            [
                RevisionResponse(
                    finding_id="X-2.1-r0-1",
                    action="implemented",
                    summary=(
                        "已经在目标小节补充跨模块依赖关系、影响路径、责任边界，"
                        "以及关联模块可以执行和复核的联合验证步骤。"
                    ),
                    changed_target_ids=["2.1.1"],
                )
            ]
            if revision
            else []
        ),
    )


def _coverage(module_id: str) -> CrossReviewCoverageEntry:
    return CrossReviewCoverageEntry(
        module_id=module_id,
        checked_dimensions=list(CROSS_REVIEW_DIMENSIONS),
    )


def _state(run_id: str) -> dict[str, object]:
    return {
        "run_id": run_id,
        "module_submissions": {
            module_id: _module(module_id) for module_id in REPORT_TAXONOMY
        },
        "specialist_submissions": {
            module_id: _module(module_id) for module_id in REPORT_TAXONOMY
        },
        "module_artifact_refs": {
            module_id: {
                "ref": f"Work/runs/{run_id}/modules/{module_id}-r0.json",
                "sha256": "0" * 64,
            }
            for module_id in REPORT_TAXONOMY
        },
    }


def test_cross_reduction_accepts_findings_closed_by_owner_recheck(
    tmp_path: Path,
) -> None:
    run_id = "cross-reduction-resolved"
    store = ReportingStore(tmp_path)
    runtime = CrossOwnerRuntime(tmp_path, store=store)
    finding = CrossReviewFinding(
        id="X-2.1-r0-1",
        owner_module_id="2.1",
        target_submodule_ids=["2.1.1"],
        related_module_ids=["2.2"],
        category="dependencies",
        impact="blocking",
        observation=(
            "首轮审查发现当前模块没有完整写出跨模块关系、影响路径、责任边界，"
            "也没有提供可由关联模块复核的联合验证步骤。"
        ),
        evidence_refs=[f"Work/runs/{run_id}/modules/2.1-r0.json"],
        required_change=(
            "在目标小节补充跨模块依赖关系、实际影响路径、明确责任边界，"
            "并写出关联模块可以直接执行和复核的联合验证步骤。"
        ),
        reviewer_checks=["确认关系和联合验证步骤已经写入目标小节。"],
    )
    revised = _module("2.1", revision=1)
    owner_input_ref = f"Work/runs/{run_id}/reviews/cross-owner-input-r0-2.1.json"
    local_review_ref = (
        f"Work/runs/{run_id}/reviews/module/cross-r1/2.1/completion-r1.json"
    )
    store.write_json(owner_input_ref, {"kind": "cross_owner_input"})
    store.write_json(
        f"Work/runs/{run_id}/modules/2.1-r1.json",
        revised.model_dump(mode="json"),
    )
    store.write_json(
        local_review_ref,
        ReviewCompletionRecord(
            lifecycle="module",
            run_id=run_id,
            reviewer_agent_id="evidence-auditor",
            reviewer_session_key="module-auditor-2.1",
            subject_refs=[f"Work/runs/{run_id}/modules/2.1-r1.json"],
            finding_refs=[],
            verdict_refs=[],
            resolved_finding_ids=[],
        ).model_dump(mode="json"),
    )
    lane = build_cross_owner_lane_completion(
        workspace=tmp_path,
        store=store,
        run_id=run_id,
        review_round=1,
        owner_module_id="2.1",
        owner_input_ref=owner_input_ref,
        revised=revised,
        cross_responses=list(revised.revision_responses),
        local_review_completion_ref=local_review_ref,
        findings=[finding],
    )
    initial_ref = f"Work/runs/{run_id}/reviews/cross-owner-findings-r0-2.1.json"
    verdict_ref = f"Work/runs/{run_id}/reviews/cross-owner-verdicts-r1-2.1.json"
    initial = CrossOwnerFindingSubmission(
        owner_module_id="2.1",
        coverage=_coverage("2.1"),
        findings=[finding],
        synthesis_inputs=[],
    )
    verdict = CrossOwnerVerdictSubmission(
        owner_module_id="2.1",
        coverage=_coverage("2.1"),
        verdicts=[
            ResolutionVerdict(
                finding_id=finding.id,
                verdict="resolved",
                reason=(
                    "修订已经完整补齐跨模块依赖关系、影响路径、责任边界，"
                    "以及关联模块可以执行和复核的联合验证步骤。"
                ),
                evidence_refs=[f"Work/runs/{run_id}/modules/2.1-r1.json"],
            )
        ],
        new_findings=[],
    )
    store.write_json(initial_ref, initial.model_dump(mode="json"))
    store.write_json(verdict_ref, verdict.model_dump(mode="json"))
    revised_pipeline = _CrossOwnerPipelineResult(
        owner_module_id="2.1",
        initial_input_ref=owner_input_ref,
        initial_result_ref=initial_ref,
        initial_result=initial,
        lane=lane,
        verdict_ref=verdict_ref,
        verdict=verdict,
        finding_refs=[initial_ref],
        verdict_refs=[verdict_ref],
        findings=[finding],
        verdicts=list(verdict.verdicts),
    ).model_dump(mode="json")
    outcomes: dict[str, object] = {
        "2.1": DeclarativeCrossOwnerPipelineOutcome(
            owner_module_id="2.1",
            status="completed",
            pipeline=revised_pipeline,
        )
    }
    for module_id in REPORT_TAXONOMY:
        if module_id == "2.1":
            continue
        no_findings = CrossOwnerFindingSubmission(
            owner_module_id=module_id,
            coverage=_coverage(module_id),
            findings=[],
            synthesis_inputs=[],
        )
        outcomes[module_id] = DeclarativeCrossOwnerPipelineOutcome(
            owner_module_id=module_id,
            status="completed",
            pipeline={
                "owner_module_id": module_id,
                "initial_result": no_findings.model_dump(mode="json"),
                "findings": [],
                "synthesis_inputs": [],
            },
        )

    reduced = runtime.reduce({"state": _state(run_id), "outcomes": outcomes})

    assert reduced["module_submissions"]["2.1"] == revised
    assert reduced["specialist_submissions"]["2.1"] == revised
    completion = ReviewCompletionRecord.model_validate_json(
        (tmp_path / reduced["cross_review_completion_ref"]).read_text(
            encoding="utf-8"
        )
    )
    assert completion.subject_refs[0].endswith("/modules/2.1-r1.json")
    assert completion.finding_refs == [
        f"Work/runs/{run_id}/reviews/cross-findings-r0.json"
    ]
    assert completion.verdict_refs == [
        f"Work/runs/{run_id}/reviews/cross-verdicts-r1.json"
    ]
    assert completion.resolved_finding_ids == [finding.id]

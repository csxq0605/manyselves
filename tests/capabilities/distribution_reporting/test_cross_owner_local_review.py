"""Characterization for the Cross-triggered local regression input boundary."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from manyselves.capabilities.distribution_reporting.domain.taxonomy import REPORT_TAXONOMY
from manyselves.capabilities.distribution_reporting.runtime.cross_local_regression import (
    build_cross_owner_local_regression_context,
)
from manyselves.capabilities.distribution_reporting.runtime.cross_owner_runtime import (
    CrossOwnerRuntime,
)
from manyselves.capabilities.distribution_reporting.runtime.models.agentic import (
    ClaimRecord,
    CrossReviewFinding,
    ModuleReviewCoverage,
    ModuleReviewFindingSubmission,
    ModuleSubmission,
    RevisionResponse,
)
from manyselves.capabilities.distribution_reporting.runtime.models.cross_owner import (
    DeclarativeCrossOwnerRuntimeContext,
)
from manyselves.capabilities.distribution_reporting.runtime.models.inputs import (
    ReviewCompletionRecord,
)
from manyselves.capabilities.distribution_reporting.runtime.models.reporting import (
    UserSupplement,
)
from manyselves.capabilities.distribution_reporting.runtime.models.review import (
    CrossOwnerRevisionAcceptance,
)
from manyselves.capabilities.distribution_reporting.runtime.storage import ReportingStore


def _module(*, revision: int, changed: bool = False) -> ModuleSubmission:
    narratives = {
        submodule_id: (
            f"修订后的内容 {submodule_id}"
            if changed and submodule_id == "2.1.1"
            else f"内容 {submodule_id}"
        )
        for submodule_id in REPORT_TAXONOMY["2.1"].submodules
    }
    claims = [
        ClaimRecord(
            id="C-2.1-1",
            module_id="2.1",
            submodule_id="2.1.1",
            text="模块关系声明",
            claim_type="technical_interpretation",
            footnote_required=False,
        )
    ]
    if changed:
        claims[0] = claims[0].model_copy(update={"text": "修订后的模块关系声明"})
    responses = (
        [
            RevisionResponse(
                finding_id="X-2.1-r1-1",
                action="implemented",
                summary="已补充关系机制、影响路径及关联模块联合验证步骤，供后续审查复核。",
                changed_target_ids=["2.1.1"],
            )
        ]
        if changed
        else []
    )
    return ModuleSubmission(
        module_id="2.1",
        submodule_narratives=narratives,
        claims=claims,
        source_ids=[],
        unresolved_questions=[],
        revision=revision,
        revision_responses=responses,
    )


def _finding() -> CrossReviewFinding:
    return CrossReviewFinding(
        id="X-2.1-r1-1",
        owner_module_id="2.1",
        target_submodule_ids=["2.1.1"],
        related_module_ids=["2.2"],
        category="dependencies",
        impact="blocking",
        observation="当前模块缺少可由关联模块复核的依赖关系机制、影响路径和明确边界说明，导致联合审查无法完成。",
        evidence_refs=["Work/runs/local-regression/modules/2.1-r0.json"],
        required_change="补充依赖关系机制、影响路径、责任边界以及与关联模块可执行的联合验证步骤。",
        reviewer_checks=["确认依赖关系和联合验证步骤已经写入目标小节。"],
    )


def _write_prior_completion(
    store: ReportingStore,
    *,
    run_id: str,
    subject_ref: str,
    completion_ref: str,
) -> None:
    store.write_json(
        completion_ref,
        ReviewCompletionRecord(
            lifecycle="module",
            run_id=run_id,
            reviewer_agent_id="evidence-auditor",
            reviewer_session_key="module-auditor-2.1",
            subject_refs=[subject_ref],
            finding_refs=[],
            verdict_refs=[],
            resolved_finding_ids=[],
        ).model_dump(mode="json"),
    )


def test_cross_local_regression_projects_prior_completion_and_revision_diff(
    tmp_path: Path,
) -> None:
    run_id = "local-regression"
    owner_module_id = "2.1"
    store = ReportingStore(tmp_path)
    baseline = _module(revision=0)
    revised = _module(revision=1, changed=True)
    baseline_ref = f"Work/runs/{run_id}/modules/{owner_module_id}-r0.json"
    completion_ref = (
        f"Work/runs/{run_id}/reviews/module/initial/{owner_module_id}/completion-r0.json"
    )
    store.write_json(baseline_ref, baseline.model_dump(mode="json"))
    _write_prior_completion(
        store,
        run_id=run_id,
        subject_ref=baseline_ref,
        completion_ref=completion_ref,
    )

    context, local_scope = build_cross_owner_local_regression_context(
        workspace=tmp_path,
        store=store,
        run_id=run_id,
        owner_module_id=owner_module_id,
        reviewed_baseline=baseline,
        revised=revised,
        findings=[_finding()],
        review_round=1,
        prior_completion_ref=completion_ref,
    )

    assert local_scope == {"2.1.1"}
    assert context.prior_review_completion_ref == completion_ref
    assert context.baseline_subject_ref == baseline_ref
    assert context.trigger_cross_findings[0].id == "X-2.1-r1-1"
    assert context.trigger_revision_responses == revised.revision_responses
    assert context.revision_diff_ref == (
        f"Work/runs/{run_id}/reviews/module/cross-r1/2.1/trigger-diff-r1.json"
    )
    assert context.revision_diff.from_revision == 0
    assert context.revision_diff.to_revision == 1
    assert context.revision_diff.changed_submodule_narratives == ["2.1.1"]
    assert context.revision_diff.changed_statement_refs == [
        "statement-" + hashlib.sha256(b"C-2.1-1").hexdigest()[:12]
    ]
    assert (tmp_path / context.revision_diff_ref).is_file()


def test_cross_local_regression_requires_bound_prior_module_completion(
    tmp_path: Path,
) -> None:
    baseline = _module(revision=0)
    revised = _module(revision=1, changed=True)

    with pytest.raises(ValueError, match="prior module review"):
        build_cross_owner_local_regression_context(
            workspace=tmp_path,
            store=ReportingStore(tmp_path),
            run_id="local-regression",
            owner_module_id="2.1",
            reviewed_baseline=baseline,
            revised=revised,
            findings=[_finding()],
            review_round=1,
            prior_completion_ref=None,
        )


def test_cross_local_regression_rejects_completion_for_another_subject(
    tmp_path: Path,
) -> None:
    run_id = "local-regression"
    baseline = _module(revision=0)
    revised = _module(revision=1, changed=True)
    store = ReportingStore(tmp_path)
    completion_ref = (
        f"Work/runs/{run_id}/reviews/module/initial/2.1/completion-r0.json"
    )
    _write_prior_completion(
        store,
        run_id=run_id,
        subject_ref=f"Work/runs/{run_id}/modules/2.1-r9.json",
        completion_ref=completion_ref,
    )

    with pytest.raises(ValueError, match="does not bind"):
        build_cross_owner_local_regression_context(
            workspace=tmp_path,
            store=store,
            run_id=run_id,
            owner_module_id="2.1",
            reviewed_baseline=baseline,
            revised=revised,
            findings=[_finding()],
            review_round=1,
            prior_completion_ref=completion_ref,
        )


@pytest.mark.asyncio
async def test_cross_runtime_prepares_and_accepts_original_auditor_local_review(
    tmp_path: Path,
) -> None:
    run_id = "local-regression-runtime"
    baseline = _module(revision=0)
    revised = _module(revision=1, changed=True)
    finding = _finding().model_copy(
        update={
            "evidence_refs": [
                f"Work/runs/{run_id}/modules/2.1-r0.json",
            ]
        }
    )
    runtime = CrossOwnerRuntime(tmp_path)
    baseline_ref = f"Work/runs/{run_id}/modules/2.1-r0.json"
    completion_ref = (
        f"Work/runs/{run_id}/reviews/module/initial/2.1/completion-r0.json"
    )
    runtime.store.write_json(baseline_ref, baseline.model_dump(mode="json"))
    _write_prior_completion(
        runtime.store,
        run_id=run_id,
        subject_ref=baseline_ref,
        completion_ref=completion_ref,
    )
    modules = {module_id: ModuleSubmission(
        module_id=module_id,
        submodule_narratives={sid: "Existing report content." for sid in spec.submodules},
        claims=[], source_ids=[], unresolved_questions=[], revision=0,
    ) for module_id, spec in REPORT_TAXONOMY.items()}
    modules["2.1"] = baseline
    current_evidence = {
        "id": "E-0002", "subject": "Labelled example", "fact": "2 completed, 1 pending",
        "module_id": "2.1", "submodule_id": "2.1.1",
        "source": {"file_id": "file-current", "path": "Inputs/example.txt"},
    }
    runtime.store.write_jsonl(f"Work/runs/{run_id}/preparation/evidence.jsonl", [current_evidence])
    changes = {"files": {"modified": ["Inputs/example.txt"]},
               "superseded_evidence_ids": ["E-0001"], "current_evidence": []}
    initial = runtime.prepare_initial({"state": {
        "run_id": run_id,
        "request": {"instruction": "保留独立验收样例及其未复核边界。"},
        "module_submissions": modules,
        "revision_input_changes": changes,
    }, "owner_module_id": "2.1"})
    accepted_initial = runtime.accept_initial({"context": initial, "result": {
        "status": "completed", "submission": {
            "owner_module_id": "2.1",
            "coverage": {"module_id": "2.1", "checked_dimensions": [
                "terminology", "facts", "risk_levels", "dependencies", "propagation", "joint_verification"]},
            "findings": [finding.model_dump(mode="json")],
        },
    }})
    author = await runtime.prepare_revision(accepted_initial)
    author_input = author.revision_preparation.prepared.revision_input
    assert author_input.report_instruction == "保留独立验收样例及其未复核边界。"
    assert author_input.input_changes.superseded_evidence_ids == ["E-0001"]
    assert [item.evidence_id for item in author_input.evidence] == ["E-0002"]
    context = DeclarativeCrossOwnerRuntimeContext(
        owner_module_id="2.1",
        preparation=initial.preparation,
        status="revision_accepted",
        revision_acceptance=CrossOwnerRevisionAcceptance(
            run_id=run_id,
            workflow_id="public-reporting",
            owner_module_id="2.1",
            review_round=1,
            owner_input_ref=f"Work/runs/{run_id}/reviews/cross-owner-input-r0-2.1.json",
            current=baseline,
            findings=[finding],
            finding_refs=[
                f"Work/runs/{run_id}/reviews/cross-owner-findings-r0-2.1.json"
            ],
            user_supplements=[
                UserSupplement(
                    id="US-local-review",
                    content="本地回归必须核验联合验证步骤。",
                    scope="submodule",
                    target_ids=["2.1.1"],
                    stages=["module_review"],
                )
            ],
            prior_completion_ref=completion_ref,
            revised=revised,
            candidate_ref=f"Work/runs/{run_id}/modules/2.1-r1.json",
        ),
    )

    prepared = await runtime.prepare_local_review(context)
    assert prepared.status == "local_review_ready"
    assert runtime.local_review_requires_agent(prepared)
    assert prepared.local_review_preparation is not None
    assert prepared.local_review_preparation.prepared is not None
    local = prepared.local_review_preparation.prepared
    assert local.lifecycle_id == "cross-r1"
    assert local.phase == "local_regression"
    assert local.reviewer_session_key == "module-auditor-2.1"
    assert local.review_input is not None
    assert local.review_input.report_instruction == "保留独立验收样例及其未复核边界。"
    assert local.review_input.input_changes.superseded_evidence_ids == ["E-0001"]
    assert prepared.local_module_context.reporting_state["report_instruction"] == author_input.report_instruction
    assert local.review_input.trigger_cross_findings == [finding]
    assert local.review_input.prior_review_completion_ref == completion_ref
    assert (
        "用户补充 US-local-review（scope=submodule; targets=2.1.1）是当前 run 的显式输入："
        "本地回归必须核验联合验证步骤。"
        in local.envelope.constraints
    )

    accepted = runtime.accept_local_review(
        {
            "context": prepared,
            "result": ModuleReviewFindingSubmission(
                coverage=ModuleReviewCoverage(submodule_ids=["2.1.1"]),
                findings=[],
            ),
        }
    )
    assert accepted.status == "local_review_accepted"
    assert accepted.local_review_acceptance is not None
    review = accepted.local_review_acceptance.review
    assert review.next_action == "completed"
    assert review.completion_ref is not None
    assert (tmp_path / review.completion_ref).is_file()

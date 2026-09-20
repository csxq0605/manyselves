"""Characterization for the Capability-owned Cross recheck boundary."""

from __future__ import annotations

from typing import Any

import pytest

from manyselves.capabilities.distribution_reporting.domain.taxonomy import (
    REPORT_TAXONOMY,
)
from manyselves.capabilities.distribution_reporting.runtime.cross_recheck import (
    accept_cross_owner_recheck,
    prepare_cross_owner_recheck,
)
from manyselves.capabilities.distribution_reporting.runtime.models.agentic import (
    CrossOwnerVerdictSubmission,
    CrossReviewFinding,
    ModuleSubmission,
    ResolutionVerdict,
    RevisionResponse,
)
from manyselves.capabilities.distribution_reporting.runtime.models.inputs import (
    CrossOwnerInput,
    CrossOwnerRelatedModuleView,
    ModuleContentView,
)
from tests.capabilities.distribution_reporting.test_cross_round import (
    _acceptance,
    _coverage,
    _finding,
)

RUN_ID = "cross-round-characterization"
OWNER_ID = "2.1"
WORKFLOW_ID = "distribution-cross-owner-2.1-pipeline"
RECHECK_REF = (
    f"Work/runs/{RUN_ID}/reviews/cross-owner-verdicts-r1-{OWNER_ID}.json"
)


def _frozen_owner_input(
    finding: CrossReviewFinding,
    *,
    lane: Any,
) -> CrossOwnerInput:
    owner = lane.module
    owner_view = ModuleContentView(
        module_id=OWNER_ID,
        revision=owner.revision,
        submodule_narratives=dict(owner.submodule_narratives),
        evidence_ids_by_submodule={
            submodule_id: [] for submodule_id in owner.submodule_narratives
        },
        unresolved_questions=[],
    )
    related_refs: dict[str, str] = {}
    related_revisions: dict[str, int] = {}
    related_views: dict[str, CrossOwnerRelatedModuleView] = {}
    for module_id in REPORT_TAXONOMY:
        if module_id == OWNER_ID:
            continue
        related = ModuleSubmission(
            module_id=module_id,
            submodule_narratives={
                submodule_id: f"关联模块内容 {submodule_id}。"
                for submodule_id in REPORT_TAXONOMY[module_id].submodules
            },
            claims=[],
            source_ids=[],
            unresolved_questions=[],
            revision=0,
        )
        ref = f"Work/runs/{RUN_ID}/modules/{module_id}-r0.json"
        related_refs[module_id] = ref
        related_revisions[module_id] = related.revision
        related_views[module_id] = CrossOwnerRelatedModuleView(
            module_id=module_id,
            revision=related.revision,
            subject_ref=ref,
            submodule_ids=list(related.submodule_narratives),
            claims=[],
            evidence_ids_by_submodule={
                submodule_id: [] for submodule_id in related.submodule_narratives
            },
            unresolved_questions=[],
        )
    return CrossOwnerInput(
        phase="recheck",
        run_id=RUN_ID,
        review_round=1,
        owner_module_id=OWNER_ID,
        review_focus=["核对跨模块依赖和联合验证关系。"],
        owner_subject_ref=lane.completion.subject.ref,
        owner_subject_revision=owner.revision,
        owner_subject=owner_view,
        owner_scope_submodule_ids=list(owner.submodule_narratives),
        related_module_refs=related_refs,
        related_module_revisions=related_revisions,
        related_module_views=related_views,
        required_findings=[finding],
        revision_responses=[
            RevisionResponse(
                finding_id=finding.id,
                action="implemented",
                summary="作者已经补充了跨模块关系证据和联合验证步骤，供 reviewer 复核。",
                changed_target_ids=["2.1.1"],
            )
        ],
        local_regression_review_ref=(
            f"Work/runs/{RUN_ID}/reviews/local-regression.json"
        ),
    )


def _verdict(finding: CrossReviewFinding) -> CrossOwnerVerdictSubmission:
    return CrossOwnerVerdictSubmission(
        owner_module_id=OWNER_ID,
        coverage=_coverage(),
        verdicts=[
            ResolutionVerdict(
                finding_id=finding.id,
                verdict="resolved",
                reason="当前修订已经满足原 finding 的全部检查要求。",
                evidence_refs=[f"Work/runs/{RUN_ID}/modules/2.1-r0.json"],
            )
        ],
        new_findings=[],
    )


def _boundary() -> tuple[Any, Any, CrossOwnerInput, CrossOwnerVerdictSubmission]:
    finding = _finding("F-1")
    initial, acceptance = _acceptance(
        finding,
        ResolutionVerdict(
            finding_id=finding.id,
            verdict="open",
            reason="修订仍然缺少能够支撑联合验证的具体关系证据。",
        ),
    )
    frozen_input = _frozen_owner_input(finding, lane=acceptance.lane)
    return initial, acceptance.lane, frozen_input, _verdict(finding)


@pytest.mark.asyncio
async def test_prepare_cross_recheck_builds_typed_envelope_and_reuses_session() -> None:
    initial, lane, frozen_input, _ = _boundary()
    stored: dict[str, object] = {}

    preparation = prepare_cross_owner_recheck(
        workflow_id=WORKFLOW_ID,
        initial=initial,
        lane=lane,
        frozen_owner_input=frozen_input,
        owner_input_ref=(
            f"Work/runs/{RUN_ID}/reviews/cross-owner-input-r1-{OWNER_ID}.json"
        ),
        review_round=1,
        required_findings=list(frozen_input.required_findings),
        read_result=stored.get,
    )

    assert preparation.mode == "invoke_agent"
    assert preparation.reviewer_session_key == initial.reviewer_session_key
    assert preparation.existing_result is None
    assert preparation.envelope is not None
    assert preparation.envelope.task_id == f"cross-owner-{OWNER_ID}-r1-recheck"
    assert preparation.envelope.run_id == RUN_ID
    assert preparation.envelope.prior_result_ref == initial.result_ref
    assert preparation.envelope.allowed_outputs == ["cross_owner_verdict_submission"]
    assert preparation.envelope.input_contract_ref == preparation.owner_input_ref
    assert preparation.envelope.target_submodule_ids == frozen_input.owner_scope_submodule_ids


@pytest.mark.asyncio
async def test_accept_cross_recheck_writes_typed_result_at_stable_ref() -> None:
    initial, lane, frozen_input, verdict = _boundary()
    stored: dict[str, object] = {}
    written: list[tuple[str, object]] = []

    preparation = prepare_cross_owner_recheck(
        workflow_id=WORKFLOW_ID,
        initial=initial,
        lane=lane,
        frozen_owner_input=frozen_input,
        owner_input_ref=(
            f"Work/runs/{RUN_ID}/reviews/cross-owner-input-r1-{OWNER_ID}.json"
        ),
        review_round=1,
        required_findings=list(frozen_input.required_findings),
        read_result=stored.get,
    )

    def write_result(ref: str, value: object) -> str:
        written.append((ref, value))
        stored[ref] = value
        return ref

    accepted = accept_cross_owner_recheck(
        preparation=preparation,
        result=verdict,
        write_immutable=write_result,
    )

    assert accepted.reviewer_session_key == initial.reviewer_session_key
    assert accepted.initial_result_ref == initial.result_ref
    assert accepted.result_ref == RECHECK_REF
    assert accepted.result is verdict
    assert written == [(RECHECK_REF, verdict)]


@pytest.mark.asyncio
async def test_prepare_and_accept_reuses_persisted_recheck_without_new_write() -> None:
    initial, lane, frozen_input, verdict = _boundary()
    stored: dict[str, object] = {RECHECK_REF: verdict.model_dump(mode="json")}
    writes: list[str] = []

    preparation = prepare_cross_owner_recheck(
        workflow_id=WORKFLOW_ID,
        initial=initial,
        lane=lane,
        frozen_owner_input=frozen_input,
        owner_input_ref=(
            f"Work/runs/{RUN_ID}/reviews/cross-owner-input-r1-{OWNER_ID}.json"
        ),
        review_round=1,
        required_findings=list(frozen_input.required_findings),
        read_result=stored.get,
    )

    def forbidden_write(ref: str, _value: object) -> str:
        writes.append(ref)
        raise AssertionError("persisted recheck must not be written again")

    accepted = accept_cross_owner_recheck(
        preparation=preparation,
        result=None,
        write_immutable=forbidden_write,
    )

    assert preparation.mode == "continue_existing"
    assert preparation.envelope is None
    assert preparation.existing_result_ref == RECHECK_REF
    assert preparation.existing_result == verdict
    assert accepted.result_ref == RECHECK_REF
    assert accepted.result == verdict
    assert accepted.reviewer_session_key == initial.reviewer_session_key
    assert writes == []

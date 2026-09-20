"""Characterization for Cross owner lane completion persistence and reuse."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from manyselves.capabilities.distribution_reporting.runtime.cross_completion import (
    build_cross_owner_lane_completion,
    promote_cross_owner_pipeline_completion,
)
from manyselves.capabilities.distribution_reporting.runtime.models.agentic import (
    CrossReviewFinding,
    ModuleSubmission,
    RevisionResponse,
)
from manyselves.capabilities.distribution_reporting.runtime.models.inputs import (
    ReviewCompletionRecord,
)
from manyselves.capabilities.distribution_reporting.runtime.storage import ReportingStore


def _module() -> ModuleSubmission:
    return ModuleSubmission(
        module_id="2.1",
        submodule_narratives={
            "2.1.1": "修订后的依赖关系机制。",
            "2.1.2": "内容 2.1.2",
            "2.1.3": "内容 2.1.3",
            "2.1.4": "内容 2.1.4",
            "2.1.5": "内容 2.1.5",
        },
        claims=[],
        source_ids=[],
        unresolved_questions=[],
        revision=1,
        revision_responses=[
            RevisionResponse(
                finding_id="X-2.1-r0-1",
                action="implemented",
                summary="已补充依赖机制和关联模块联合验证步骤，供本轮复核。",
                changed_target_ids=["2.1.1"],
            )
        ],
    )


def _finding() -> CrossReviewFinding:
    return CrossReviewFinding(
        id="X-2.1-r0-1",
        owner_module_id="2.1",
        target_submodule_ids=["2.1.1"],
        related_module_ids=["2.2"],
        category="dependencies",
        impact="blocking",
        observation="当前模块缺少可由关联模块复核的依赖关系机制、影响路径和明确边界说明，导致联合审查无法完成。",
        evidence_refs=["Work/runs/lane-completion/modules/2.1-r1.json"],
        required_change="补充依赖关系机制、影响路径、责任边界以及与关联模块可执行的联合验证步骤。",
        reviewer_checks=["确认依赖关系和联合验证步骤已经写入目标小节。"],
    )


def _write_artifacts(store: ReportingStore, run_id: str, module: ModuleSubmission) -> tuple[str, str, str]:
    owner_input_ref = f"Work/runs/{run_id}/reviews/cross-owner-input-r0-2.1.json"
    subject_ref = f"Work/runs/{run_id}/modules/2.1-r{module.revision}.json"
    local_review_ref = (
        f"Work/runs/{run_id}/reviews/module/cross-r1/2.1/completion-r{module.revision}.json"
    )
    store.write_json(owner_input_ref, {"kind": "cross_owner_input"})
    store.write_json(subject_ref, module.model_dump(mode="json"))
    store.write_json(
        local_review_ref,
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
    return owner_input_ref, subject_ref, local_review_ref


def test_cross_owner_lane_completion_persists_existing_artifact_format(
    tmp_path: Path,
) -> None:
    run_id = "lane-completion"
    module = _module()
    store = ReportingStore(tmp_path)
    owner_input_ref, subject_ref, local_review_ref = _write_artifacts(store, run_id, module)
    responses = list(module.revision_responses)
    findings = [_finding()]

    lane = build_cross_owner_lane_completion(
        workspace=tmp_path,
        store=store,
        run_id=run_id,
        review_round=1,
        owner_module_id="2.1",
        owner_input_ref=owner_input_ref,
        revised=module,
        cross_responses=responses,
        local_review_completion_ref=local_review_ref,
        findings=findings,
    )

    expected_completion_ref = (
        f"Work/runs/{run_id}/lanes/cross-r1/module-2.1/completion-r1.json"
    )
    assert lane.module == module
    assert lane.responses == responses
    assert lane.local_review_ref == local_review_ref
    assert lane.completion_ref == expected_completion_ref
    assert lane.completion.lane_id == "cross-r1-module-2.1"
    assert lane.completion.run_id == run_id
    assert lane.completion.stage == "cross"
    assert lane.completion.review_round == 1
    assert lane.completion.module_id == "2.1"
    assert lane.completion.status == "completed"
    assert lane.completion.owner_input is not None
    assert lane.completion.owner_input.ref == owner_input_ref
    assert lane.completion.subject is not None
    assert lane.completion.subject.ref == subject_ref
    assert lane.completion.local_review_completion is not None
    assert lane.completion.local_review_completion.ref == local_review_ref
    assert lane.completion.subject_revision == module.revision
    assert lane.completion.reviewer_session_id == "cross-owner-2.1"
    assert lane.completion.lease_epoch == 1
    semantic_payload = {
        "run_id": run_id,
        "review_round": 1,
        "module_id": "2.1",
        "owner_input_ref": owner_input_ref,
        "findings": [finding.model_dump(mode="json") for finding in findings],
        "schema_version": "1",
    }
    expected_semantic_key = hashlib.sha256(
        json.dumps(
            semantic_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    assert lane.completion.semantic_key == expected_semantic_key
    assert (tmp_path / expected_completion_ref).is_file()


def test_cross_owner_lane_completion_reuses_typed_existing_completion(
    tmp_path: Path,
) -> None:
    run_id = "lane-completion-resume"
    module = _module()
    store = ReportingStore(tmp_path)
    owner_input_ref, _subject_ref, local_review_ref = _write_artifacts(store, run_id, module)
    kwargs = {
        "workspace": tmp_path,
        "store": store,
        "run_id": run_id,
        "review_round": 1,
        "owner_module_id": "2.1",
        "owner_input_ref": owner_input_ref,
        "revised": module,
        "cross_responses": list(module.revision_responses),
        "local_review_completion_ref": local_review_ref,
        "findings": [_finding()],
    }

    first = build_cross_owner_lane_completion(**kwargs)
    resumed = build_cross_owner_lane_completion(**kwargs)

    assert resumed.completion == first.completion
    assert resumed.completion_ref == first.completion_ref


def test_cross_owner_lane_completion_rejects_changed_existing_business_identity(
    tmp_path: Path,
) -> None:
    run_id = "lane-completion-changed"
    module = _module()
    store = ReportingStore(tmp_path)
    owner_input_ref, _subject_ref, local_review_ref = _write_artifacts(store, run_id, module)
    kwargs = {
        "workspace": tmp_path,
        "store": store,
        "run_id": run_id,
        "review_round": 1,
        "owner_module_id": "2.1",
        "owner_input_ref": owner_input_ref,
        "revised": module,
        "cross_responses": list(module.revision_responses),
        "local_review_completion_ref": local_review_ref,
        "findings": [_finding()],
    }
    first = build_cross_owner_lane_completion(**kwargs)
    payload = first.completion.model_dump(mode="json")
    payload["lane_id"] = "cross-r1-module-2.2"
    store.write_json(first.completion_ref, payload)

    with pytest.raises(ValueError, match="business identity"):
        build_cross_owner_lane_completion(**kwargs)


def test_cross_owner_pipeline_promotion_preserves_terminal_refs(tmp_path: Path) -> None:
    run_id = "lane-completion-promotion"
    module = _module()
    store = ReportingStore(tmp_path)
    owner_input_ref, _subject_ref, local_review_ref = _write_artifacts(
        store,
        run_id,
        module,
    )
    initial_result_ref = (
        f"Work/runs/{run_id}/reviews/cross-owner-findings-r0-2.1.json"
    )
    verdict_ref = f"Work/runs/{run_id}/reviews/cross-owner-verdicts-r1-2.1.json"
    store.write_json(initial_result_ref, {"kind": "cross_owner_finding_submission"})
    store.write_json(verdict_ref, {"kind": "cross_owner_verdict_submission"})
    lane = build_cross_owner_lane_completion(
        workspace=tmp_path,
        store=store,
        run_id=run_id,
        review_round=1,
        owner_module_id="2.1",
        owner_input_ref=owner_input_ref,
        revised=module,
        cross_responses=list(module.revision_responses),
        local_review_completion_ref=local_review_ref,
        findings=[_finding()],
    )

    promoted = promote_cross_owner_pipeline_completion(
        workspace=tmp_path,
        store=store,
        lane=lane,
        initial_result_ref=initial_result_ref,
        verdict_ref=verdict_ref,
    )

    assert promoted.completion_ref == (
        f"Work/runs/{run_id}/lanes/cross-r1/module-2.1/pipeline-completion.json"
    )
    assert promoted.completion.review_round == 1
    assert promoted.completion.initial_result is not None
    assert promoted.completion.initial_result.ref == initial_result_ref
    assert promoted.completion.verdict_result is not None
    assert promoted.completion.verdict_result.ref == verdict_ref
    assert promoted.completion.schema_version == "3"

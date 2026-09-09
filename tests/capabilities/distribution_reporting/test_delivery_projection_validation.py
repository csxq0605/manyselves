"""Characterize Capability-owned Delivery projection and structure validation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from manyselves.capabilities.distribution_reporting.domain.taxonomy import (
    REPORT_TAXONOMY,
    compose_module_markdown,
)
from manyselves.capabilities.distribution_reporting.runtime.delivery_projection import (
    build_delivery_projection,
)
from manyselves.capabilities.distribution_reporting.runtime.models.agentic import (
    ClaimRecord,
    EditedReportSubmission,
    ModuleSubmission,
)
from manyselves.capabilities.distribution_reporting.runtime.models.inputs import (
    ReviewCompletionRecord,
    ValidationReport,
)
from manyselves.capabilities.distribution_reporting.runtime.models.reporting import (
    REPORT_MODULE_IDS,
)
from manyselves.capabilities.distribution_reporting.runtime.rendering.pds_docx_renderer import (
    PdsDocxRenderer,
)
from manyselves.capabilities.distribution_reporting.runtime.report_validation import (
    validate_final_report_structure,
)
from manyselves.capabilities.distribution_reporting.runtime.review_artifacts import (
    load_current_review_completion,
    validated_final_audit_subject,
)
from manyselves.capabilities.distribution_reporting.runtime.storage import ReportingStore


def _edited_report() -> EditedReportSubmission:
    return EditedReportSubmission(
        title="Report",
        assessment_background="background",
        findings_overview="overview",
        regional_executive_summary="summary",
        module_narratives={
            module_id: compose_module_markdown(
                module_id,
                {
                    submodule_id: f"module {module_id} {submodule_id}"
                    for submodule_id in definition.submodules
                },
            )
            for module_id, definition in REPORT_TAXONOMY.items()
        },
        risk_panorama="panorama",
        dimension_risk_analysis="risk analysis",
        data_gap_analysis="gaps",
        improvement_action_plan="actions",
    )


def _claims() -> list[ClaimRecord]:
    return [
        ClaimRecord(
            id=f"C-{index}",
            module_id=module_id,
            submodule_id=next(iter(REPORT_TAXONOMY[module_id].submodules)),
            text=f"claim {module_id}",
            claim_type="recommendation",
            unresolved=True,
        )
        for index, module_id in enumerate(REPORT_MODULE_IDS, start=1)
    ]


def _state(run_id: str, edited: EditedReportSubmission) -> dict:
    claims = _claims()
    module_submissions = {
        module_id: ModuleSubmission(
            module_id=module_id,
            submodule_narratives={
                submodule_id: f"module {module_id} {submodule_id}"
                for submodule_id in REPORT_TAXONOMY[module_id].submodules
            },
            claims=[claim for claim in claims if claim.module_id == module_id],
            source_ids=[],
            unresolved_questions=[],
            revision=0,
        )
        for module_id in REPORT_MODULE_IDS
    }
    return {
        "run_id": run_id,
        "edited_report": edited,
        "module_submissions": module_submissions,
        "evidence_items": [],
        "photo_assets": [],
    }


def test_capability_projection_builds_the_canonical_renderer_markdown(
    tmp_path: Path,
) -> None:
    edited = _edited_report()
    claims = _claims()
    state = _state("run-projection-characterization", edited)
    actual_report, actual_markdown = build_delivery_projection(
        tmp_path,
        state,
        edited,
        claims,
    )

    assert actual_report.module_narratives == edited.module_narratives
    assert actual_report.ledger.claims == claims
    assert actual_markdown == PdsDocxRenderer._compose_markdown(actual_report)


@pytest.mark.parametrize("carrier", ["evidence_items", "photo_assets"])
def test_final_completion_restores_json_roundtripped_asset_carriers(
    tmp_path: Path, carrier: str,
) -> None:
    from manyselves.capabilities.distribution_reporting.runtime.final_review_tools import (
        FinalReviewTools,
    )
    from manyselves.capabilities.distribution_reporting.runtime.models.final_review import (
        DeclarativeFinalReviewContext,
    )
    from manyselves.capabilities.distribution_reporting.runtime.models.reporting import (
        EvidenceItem,
        PhotoAsset,
    )

    run_id = f"final-restored-{carrier}"
    edited = _edited_report()
    state = _state(run_id, edited)
    assets = {
        "evidence_items": EvidenceItem.model_validate({
            "id": "E-0001", "subject": "source fact", "fact": "measured",
            "source": {"file_id": "source", "path": "Inputs/source.xlsx"},
        }),
        "photo_assets": PhotoAsset.model_validate({
            "id": "P-0001", "path": "Work/unused.png", "sha256": "test-digest",
            "media_type": "image/png", "source_member": "xl/media/image1.png",
        }),
    }
    state[carrier] = [assets[carrier]]
    if carrier == "photo_assets":
        state["evidence_items"] = [assets["evidence_items"].model_copy(update={
            "module_id": "2.1", "submodule_id": "2.1.1",
            "photo_refs": ["P-0001"],
        })]
        edited.photo_ids = ["P-0001"]
        photo_path = tmp_path / assets["photo_assets"].path
        photo_path.parent.mkdir(parents=True, exist_ok=True)
        photo_path.write_bytes(b"fixture image: projection only checks local presence")
        from manyselves.capabilities.distribution_reporting.runtime.source_ledger import (
            SourceLedger,
        )

        SourceLedger(tmp_path, run_id).register_project(
            "E-0001", "source fact", "Inputs/source.xlsx", "measured",
        )
    review = DeclarativeFinalReviewContext(
        state=state, current=edited,
        subject_ref=f"Work/runs/{run_id}/edited-revisions/chief-r0.json",
        findings_by_chapter={}, pending_by_chapter={}, initial_lane_refs={},
    )
    serialized = json.loads(review.model_dump_json())
    assert isinstance(serialized["state"][carrier][0], dict)

    completed = FinalReviewTools(workspace=tmp_path).complete_review(serialized)

    assert completed[carrier] == [assets[carrier]]
    assert (tmp_path / completed["final_audit_snapshot_ref"]).is_file()
    assert isinstance(serialized["state"][carrier][0], dict)


@pytest.mark.parametrize("invalid_suffix", ["\n# 99. unexpected\n"])
def test_capability_validation_persists_same_success_and_failure_reports(
    tmp_path: Path,
    invalid_suffix: str,
) -> None:
    edited = _edited_report()
    state = _state("run-validation-characterization", edited)
    _report, markdown = build_delivery_projection(
        tmp_path,
        state,
        edited,
        _claims(),
    )
    store = ReportingStore(tmp_path)

    validate_final_report_structure(
        store=store,
        state=state,
        markdown=markdown,
        phase="delivery-final",
    )
    success_ref = (
        tmp_path
        / "Work/runs/run-validation-characterization/reviews/report-integrity-delivery-final.json"
    )
    success_report = ValidationReport.model_validate_json(
        success_ref.read_text(encoding="utf-8")
    )
    assert success_report.passed is True
    assert (
        tmp_path
        / "Work/runs/run-validation-characterization/validation/report-delivery-final.md"
    ).read_text(encoding="utf-8") == markdown

    invalid_state = _state("run-validation-characterization-invalid", edited)
    invalid_store = ReportingStore(tmp_path)
    with pytest.raises(ValueError, match="总报告完整性校验未通过"):
        validate_final_report_structure(
            store=invalid_store,
            state=invalid_state,
            markdown=markdown + invalid_suffix,
            phase="delivery-final",
        )
    failure_ref = (
        tmp_path
        / "Work/runs/run-validation-characterization-invalid/reviews/report-integrity-delivery-final.json"
    )
    failure_report = ValidationReport.model_validate_json(
        failure_ref.read_text(encoding="utf-8")
    )
    assert failure_report.passed is False
    assert failure_report.failures
    assert (
        tmp_path
        / "Work/runs/run-validation-characterization-invalid/validation/report-delivery-final.md"
    ).read_text(encoding="utf-8") == markdown + invalid_suffix


def test_final_chapter_wave_loader_merges_verdict_new_findings(
    tmp_path: Path,
) -> None:
    run_id = "run-final-chapter-wave-loader"
    subject_ref = f"Work/runs/{run_id}/edited-revisions/chief-r0.json"
    finding_ref = f"Work/runs/{run_id}/reviews/final-chapter-lane-1-r0.json"
    verdict_ref = f"Work/runs/{run_id}/reviews/final-chapter-lane-1-r1.json"
    follow_up_verdict_ref = (
        f"Work/runs/{run_id}/reviews/final-chapter-lane-1-r2.json"
    )
    completion_ref = f"Work/runs/{run_id}/reviews/final-completion.json"
    store = ReportingStore(tmp_path)
    store.write_json(subject_ref, {"kind": "subject"})
    store.write_json(
        finding_ref,
        {
            "kind": "final_chapter_lane_finding_submission",
            "findings": [{"id": "F-1"}],
        },
    )
    store.write_json(
        verdict_ref,
        {
            "kind": "final_chapter_lane_verdict_submission",
            "verdicts": [{"finding_id": "F-1"}],
            "new_findings": [{"id": "F-2"}],
        },
    )
    store.write_json(
        follow_up_verdict_ref,
        {
            "kind": "final_chapter_lane_verdict_submission",
            "verdicts": [{"finding_id": "F-2"}],
            "new_findings": [],
        },
    )
    store.write_json(
        completion_ref,
        ReviewCompletionRecord(
            lifecycle="final",
            run_id=run_id,
            reviewer_agent_id="chief-editor-auditor",
            reviewer_session_key="final-chapter-wave",
            subject_refs=[subject_ref],
            finding_refs=[finding_ref],
            verdict_refs=[verdict_ref, follow_up_verdict_ref],
            resolved_finding_ids=["F-1", "F-2"],
        ).model_dump(mode="json"),
    )

    completion, artifacts = load_current_review_completion(
        workspace=tmp_path,
        run_id=run_id,
        completion_ref=completion_ref,
        lifecycle="final",
        reviewer_agent_id="chief-editor-auditor",
        reviewer_session_key="final-chapter-wave",
    )

    assert completion.reviewer_session_key == "final-chapter-wave"
    assert [artifact["kind"] for artifact in artifacts] == [
        "final_chapter_lane_finding_submission",
        "final_chapter_lane_verdict_submission",
        "final_chapter_lane_verdict_submission",
    ]


def test_final_audit_reconstructs_missing_snapshot_without_provider_boundary(
    tmp_path: Path,
) -> None:
    run_id = "run-final-audit-capability"
    edited = _edited_report()
    state = _state(run_id, edited)
    subject_ref = f"Work/runs/{run_id}/edited-revisions/chief-r0.json"
    completion_ref = f"Work/runs/{run_id}/reviews/final-completion.json"
    state["final_review_completion_ref"] = completion_ref
    store = ReportingStore(tmp_path)
    store.write_json(subject_ref, edited.model_dump(mode="json"))
    store.write_json(
        completion_ref,
        ReviewCompletionRecord(
            lifecycle="final",
            run_id=run_id,
            reviewer_agent_id="chief-editor-auditor",
            reviewer_session_key="chief-editor-auditor",
            subject_refs=[subject_ref],
            finding_refs=[],
            verdict_refs=[],
            resolved_finding_ids=[],
        ).model_dump(mode="json"),
    )

    audited, snapshot_ref = validated_final_audit_subject(
        workspace=tmp_path,
        store=store,
        state=state,
    )

    assert audited == edited
    assert snapshot_ref == f"Work/runs/{run_id}/reviews/final-audit-snapshot.json"
    snapshot = json.loads((tmp_path / snapshot_ref).read_text(encoding="utf-8"))
    assert snapshot["subject_ref"] == subject_ref
    assert snapshot["completion_ref"] == completion_ref
    assert (
        tmp_path / f"Work/runs/{run_id}/validation/report-final-audit-legacy.md"
    ).is_file()
    assert (
        tmp_path
        / f"Work/runs/{run_id}/reviews/report-integrity-final-audit-legacy.json"
    ).is_file()

    tampered = {
        **state,
        "edited_report": edited.model_copy(update={"title": "tampered after audit"}),
    }
    with pytest.raises(ValueError, match="changed after final audit"):
        validated_final_audit_subject(
            workspace=tmp_path,
            store=store,
            state=tampered,
        )

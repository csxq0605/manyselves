"""Characterize Capability-owned Delivery projection and structure validation."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

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
)
from manyselves.capabilities.distribution_reporting.runtime.models.inputs import (
    ValidationReport,
)
from manyselves.capabilities.distribution_reporting.runtime.models.reporting import (
    REPORT_MODULE_IDS,
)
from manyselves.capabilities.distribution_reporting.runtime.report_validation import (
    validate_final_report_structure,
)
from manyselves.capabilities.distribution_reporting.runtime.storage import ReportingStore
from manyselves.core.reporting.workflow import ReportWorkflowRunner


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
    return {
        "run_id": run_id,
        "edited_report": edited,
        "evidence_items": [],
        "photo_assets": [],
    }


def test_capability_projection_matches_current_runner_projection(
    tmp_path: Path,
) -> None:
    edited = _edited_report()
    claims = _claims()
    state = _state("run-projection-characterization", edited)
    runner = SimpleNamespace(service=SimpleNamespace(workspace=tmp_path))

    expected_report, expected_markdown = ReportWorkflowRunner._delivery_projection(
        runner,
        state,
        edited,
        claims,
    )
    actual_report, actual_markdown = build_delivery_projection(
        tmp_path,
        state,
        edited,
        claims,
    )

    assert actual_report.model_dump(mode="json") == expected_report.model_dump(mode="json")
    assert actual_markdown == expected_markdown


@pytest.mark.parametrize("invalid_suffix", ["\n# 99. unexpected\n"])
def test_capability_validation_persists_same_success_and_failure_reports(
    tmp_path: Path,
    invalid_suffix: str,
) -> None:
    edited = _edited_report()
    state = _state("run-validation-characterization", edited)
    markdown = ReportWorkflowRunner._canonical_markdown(edited)
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

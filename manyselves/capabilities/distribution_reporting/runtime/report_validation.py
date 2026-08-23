"""Capability-owned deterministic final-report structure validation."""

from __future__ import annotations

from typing import Any

from .assets import validate_final_report_markdown
from .models.agentic import EditedReportSubmission
from .models.inputs import ValidationFailure, ValidationReport
from .storage import ReportingStore


def validate_final_report_structure(
    *,
    store: ReportingStore,
    state: dict[str, Any],
    markdown: str,
    phase: str,
) -> None:
    """Persist the structural report and raise ``ValueError`` on failure."""

    run_id = state["run_id"]
    validation_ref = f"Work/runs/{run_id}/reviews/report-integrity-{phase}.json"
    subject_ref = f"Work/runs/{run_id}/validation/report-{phase}.md"
    store.write_text(subject_ref, markdown)
    revision_text = phase.removeprefix("chief-candidate-r")
    subject_revision = (
        int(revision_text)
        if phase.startswith("chief-candidate-r") and revision_text.isdigit()
        else None
    )
    check_ids = ["final_report.fixed_sections_and_markdown"]
    try:
        edited = state.get("edited_report")
        plan = (
            edited.special_topic_plan
            if isinstance(edited, EditedReportSubmission)
            else state.get("special_topic_plan")
        )
        validate_final_report_markdown(markdown, plan)
    except ValueError as exc:
        store.write_json(
            validation_ref,
            ValidationReport(
                validation_protocol_version=2,
                run_id=run_id,
                subject_ref=subject_ref,
                subject_revision=subject_revision,
                validator="final-report-structure/v2",
                check_ids=check_ids,
                failures=[
                    ValidationFailure(
                        check_id=check_ids[0],
                        target_path="markdown",
                        message=str(exc),
                    )
                ],
                passed=False,
            ).model_dump(mode="json"),
        )
        raise ValueError(f"总报告完整性校验未通过：{exc}") from exc
    store.write_json(
        validation_ref,
        ValidationReport(
            validation_protocol_version=2,
            run_id=run_id,
            subject_ref=subject_ref,
            subject_revision=subject_revision,
            validator="final-report-structure/v2",
            check_ids=check_ids,
            passed=True,
        ).model_dump(mode="json"),
    )


__all__ = ["validate_final_report_structure"]

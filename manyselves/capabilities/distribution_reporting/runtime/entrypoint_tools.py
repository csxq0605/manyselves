"""Deterministic projections between top-level Reporting workflow contexts."""

from __future__ import annotations

from typing import Any

from .models.entrypoint import (
    ReportingPreparationAttachment,
    ReportingRunContext,
    ReportingRunInitializerInput,
)
from .models.reporting import REPORT_MODULE_IDS


def initialize_reporting_run(value: Any) -> ReportingRunContext:
    """Bind the generic Run identity to the validated public request."""

    inputs = ReportingRunInitializerInput.model_validate(value)
    return ReportingRunContext(
        run_id=inputs.run_id,
        request=inputs.request,
        preparation_context={
            "run_id": inputs.run_id,
            "request": inputs.request,
        },
    )


def attach_preparation(value: Any) -> ReportingRunContext:
    """Attach the exact result returned by the Preparation subworkflow."""

    inputs = ReportingPreparationAttachment.model_validate(value)
    return inputs.context.model_copy(
        update={
            "preparation_context": inputs.preparation,
        },
    )


def build_reporting_state(context: ReportingRunContext) -> dict[str, Any]:
    """Project typed Preparation output to the existing stage state shape."""

    preparation = context.preparation_context
    requested_modules = tuple(context.request.target_modules)
    return {
        "run_id": context.run_id,
        "request": context.request,
        "resume": preparation.resume,
        "input_snapshot_ref": preparation.input_snapshot_ref,
        "input_snapshot_digest": preparation.input_snapshot_digest,
        "project_manifest": preparation.project_manifest,
        "preparation_worker_results": preparation.preparation_worker_results,
        "parsed_artifacts": preparation.parsed_artifacts,
        "preparation_parallelism": preparation.preparation_parallelism,
        "evidence_items": preparation.evidence_items,
        "photo_assets": preparation.photo_assets,
        "photo_evidence_adjacency": preparation.photo_evidence_adjacency,
        "mapping_gaps": preparation.mapping_gaps,
        "coverage_matrix": preparation.coverage_matrix,
        "report_taxonomy": preparation.report_taxonomy,
        "special_topic_plan": preparation.special_topic_plan,
        "preparation_refs": preparation.preparation_refs,
        "preparation_completion_ref": preparation.preparation_completion_ref,
        "evidence_index_ref": preparation.evidence_index_ref,
        "source_ledger_ref": preparation.source_ledger_ref,
        "requested_modules": requested_modules,
        "full_report": set(requested_modules) == set(REPORT_MODULE_IDS),
    }


def build_entrypoint_tool_implementations() -> dict[str, Any]:
    """Bind the typed projections used by full/module entrypoint workflows."""

    return {
        "initialize-reporting-run": initialize_reporting_run,
        "attach-reporting-preparation": attach_preparation,
        "build-reporting-state": build_reporting_state,
    }


__all__ = [
    "attach_preparation",
    "build_entrypoint_tool_implementations",
    "build_reporting_state",
    "initialize_reporting_run",
]

"""Typed values for the first aggregate-existing workflow slice."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from .agentic import EditedReportSubmission, ModuleSubmission
from .inputs import AggregateEditorInput, RevisionInputChanges
from .preparation import PreparationContext
from .reporting import EvidenceItem, PhotoAsset, ReportingModel, ReportRequest

ModuleId = Literal["2.1", "2.2", "2.3", "2.4", "2.5"]


class AggregateExistingPreparationInput(ReportingModel):
    """Generic Host input for preparing an existing-module aggregate run."""

    run_id: str = Field(min_length=1)
    request: ReportRequest


class AggregateExistingContext(ReportingModel):
    """Frozen source projection handed to the aggregate editor task."""

    kind: Literal["aggregate_existing_context"] = "aggregate_existing_context"
    run_id: str = Field(min_length=1)
    request: ReportRequest
    source_format: Literal["structured_module", "markdown"]
    module_refs: dict[ModuleId, str]
    structured_modules: dict[ModuleId, ModuleSubmission] = Field(default_factory=dict)
    markdown_modules: dict[ModuleId, str] = Field(default_factory=dict)
    evidence_items: list[EvidenceItem] = Field(default_factory=list)
    photo_assets: list[PhotoAsset] = Field(default_factory=list)
    module_review_completion_refs: dict[ModuleId, str] = Field(default_factory=dict)
    preparation_context: PreparationContext | None = None
    revision_input_changes: RevisionInputChanges | None = None
    report_instruction: str = ""
    source_manifest_ref: str = Field(min_length=1)
    integrity_report_ref: str = Field(min_length=1)
    editor_input_ref: str = Field(min_length=1)
    editor_input: AggregateEditorInput


class AggregateExistingHandoff(ReportingModel):
    """Typed handoff from the aggregate editor to Final and Delivery stages."""

    kind: Literal["aggregate_existing_handoff"] = "aggregate_existing_handoff"
    context: AggregateExistingContext
    edited_report: EditedReportSubmission


__all__ = [
    "AggregateExistingContext",
    "AggregateExistingHandoff",
    "AggregateExistingPreparationInput",
    "ModuleId",
]

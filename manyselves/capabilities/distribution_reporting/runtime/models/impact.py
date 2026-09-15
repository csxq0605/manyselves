"""Typed impact-analysis carriers for the revise-report workflow."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from .agentic import StrictModel


class RevisionImpactEvidenceChange(StrictModel):
    """One materialized input change compared against the baseline Run."""

    kind: Literal["added", "superseded", "file_added", "file_modified", "file_removed"]
    evidence_id: str | None = None
    superseded_evidence_id: str | None = None
    title: str = ""
    locator: str = ""
    summary: str = ""


class RevisionImpactItem(StrictModel):
    """One likely affected subsection with a proposed instruction."""

    subsection_id: str = Field(min_length=3)
    module_id: str
    reason: str = Field(min_length=1)
    suggested_instruction: str = Field(min_length=1)
    confidence: Literal["high", "medium", "low"] = "medium"
    evidence_ids: list[str] = Field(default_factory=list)
    change_kinds: list[str] = Field(default_factory=list)


class RevisionImpactAnalysis(StrictModel):
    """Business artifact: what changed and which subsections should be revised."""

    kind: Literal["revision_impact_analysis"] = "revision_impact_analysis"
    run_id: str
    baseline_run_id: str
    instruction: str
    impact_ref: str = ""
    changes: list[RevisionImpactEvidenceChange] = Field(default_factory=list)
    impacts: list[RevisionImpactItem] = Field(default_factory=list)
    requested_changes: dict[str, str] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)

    @property
    def target_modules(self) -> list[str]:
        return sorted({".".join(key.split(".")[:2]) for key in self.requested_changes})


class RevisionImpactDecision(StrictModel):
    """User confirmation of all or part of the impact list."""

    kind: Literal["revision_impact_decision"] = "revision_impact_decision"
    action: Literal["accept_all", "accept_selected", "abort"]
    selected_subsection_ids: list[str] = Field(default_factory=list)
    overridden_instructions: dict[str, str] = Field(default_factory=dict)
    comment: str = ""


__all__ = [
    "RevisionImpactAnalysis",
    "RevisionImpactDecision",
    "RevisionImpactEvidenceChange",
    "RevisionImpactItem",
]

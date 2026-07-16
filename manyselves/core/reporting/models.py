"""Typed carriers shared by the power-distribution reporting workflow."""

from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .taxonomy import resolve_submodule

REPORT_MODULE_IDS = ("2.1", "2.2", "2.3", "2.4", "2.5")


class ReportingModel(BaseModel):
    """Strict base model for persisted workflow state."""

    model_config = ConfigDict(extra="forbid")


class SourceLocation(ReportingModel):
    file_id: str = Field(min_length=1)
    path: Path
    sheet: str | None = None
    cell: str | None = None
    page: int | None = Field(default=None, ge=1)
    row: int | None = Field(default=None, ge=1)
    column: str | None = None
    image_id: str | None = None

    @field_validator("path", mode="before")
    @classmethod
    def path_must_not_be_blank(cls, value: Any) -> Any:
        if not str(value).strip() or Path(value) == Path("."):
            raise ValueError("source path must not be blank")
        return value


class ReportRequest(ReportingModel):
    instruction: str = Field(min_length=1)
    target_modules: list[str] = Field(default_factory=lambda: list(REPORT_MODULE_IDS))
    execution_requirements: list[str] = Field(default_factory=list)
    missing_evidence_policy: Literal["ask", "block", "skip", "draft"] = "ask"

    @field_validator("target_modules")
    @classmethod
    def target_modules_are_fixed(cls, values: list[str]) -> list[str]:
        unknown = sorted(set(values) - set(REPORT_MODULE_IDS))
        if unknown:
            raise ValueError(f"module ids must be within 2.1-2.5; got {unknown}")
        return values


EvidenceDecisionAction = Literal["supplement", "draft", "skip", "stop"]


class EvidenceDecisionRequest(ReportingModel):
    decision_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    missing_items: list[str] = Field(min_length=1)
    affected_modules: list[str] = Field(min_length=1)
    allowed_actions: list[EvidenceDecisionAction] = Field(
        default_factory=lambda: ["supplement", "draft", "skip", "stop"]
    )
    status: Literal["pending", "resolved"] = "pending"
    selected_action: EvidenceDecisionAction | None = None
    user_notes: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    resolved_at: datetime | None = None

    @field_validator("affected_modules")
    @classmethod
    def affected_modules_are_fixed(cls, values: list[str]) -> list[str]:
        unknown = sorted(set(values) - set(REPORT_MODULE_IDS))
        if unknown:
            raise ValueError(f"affected module ids must be within 2.1-2.5; got {unknown}")
        return values

    @model_validator(mode="after")
    def resolution_fields_match_status(self) -> "EvidenceDecisionRequest":
        if self.status == "pending" and (self.selected_action is not None or self.resolved_at):
            raise ValueError("pending evidence decision cannot contain resolution fields")
        if self.status == "resolved" and (self.selected_action is None or self.resolved_at is None):
            raise ValueError("resolved evidence decision requires action and resolved_at")
        return self


class RevisionRequest(ReportingModel):
    baseline_version_id: str = Field(min_length=1)
    feedback: str = Field(min_length=1)
    target_module_ids: list[Literal["2.1", "2.2", "2.3", "2.4", "2.5"]] = Field(min_length=1)
    target_submodule_ids: list[str] = Field(default_factory=list)
    target_claim_ids: list[str] = Field(default_factory=list)
    promote_to_skill: bool = False
    promote_skill_id: str | None = None

    @model_validator(mode="after")
    def targets_stay_in_selected_modules(self) -> "RevisionRequest":
        for submodule_id in self.target_submodule_ids:
            if resolve_submodule(submodule_id).module_id not in self.target_module_ids:
                raise ValueError("revision submodule is outside selected modules")
        if self.promote_to_skill and not (self.promote_skill_id or "").strip():
            raise ValueError("explicit Skill promotion requires promote_skill_id")
        if self.promote_to_skill and len(self.target_module_ids) != 1:
            raise ValueError("one Skill promotion must target exactly one module")
        return self


class ScopeExpansionRequest(ReportingModel):
    request_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    baseline_version_id: str = Field(min_length=1)
    requested_module_ids: list[str]
    requested_submodule_ids: list[str]
    unexpected_module_ids: list[str] = Field(default_factory=list)
    unexpected_submodule_ids: list[str] = Field(default_factory=list)
    unexpected_claim_ids: list[str] = Field(default_factory=list)
    reason: str = Field(min_length=1)
    status: Literal["pending"] = "pending"


class ManifestFile(ReportingModel):
    id: str = Field(min_length=1)
    path: Path
    sha256: str = Field(min_length=1)
    media_type: str = Field(min_length=1)
    purpose: str | None = None
    parse_status: Literal["pending", "parsed", "failed"] = "pending"
    error: str | None = None


class ProjectManifest(ReportingModel):
    files: list[ManifestFile] = Field(default_factory=list)


class ParsedArtifact(ReportingModel):
    id: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    source: SourceLocation
    payload: dict[str, Any]


class PhotoAsset(ReportingModel):
    id: str = Field(min_length=1)
    path: Path
    sha256: str = Field(min_length=1)
    media_type: str = Field(min_length=1)
    source_member: str = Field(min_length=1)


class EvidenceItem(ReportingModel):
    id: str = Field(min_length=1)
    subject: str = Field(min_length=1)
    fact: str = Field(min_length=1)
    source: SourceLocation
    value: str | float | int | None = None
    unit: str | None = None
    observed_at: str | None = None
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    needs_confirmation: bool = False
    module_id: str | None = None
    submodule_id: str | None = None
    photo_refs: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def submodule_matches_module(self) -> "EvidenceItem":
        if self.submodule_id is None:
            return self
        submodule = resolve_submodule(self.submodule_id)
        if self.module_id is None:
            raise ValueError("module_id is required when submodule_id is set")
        if submodule.module_id != self.module_id:
            raise ValueError(
                f"submodule {self.submodule_id} does not belong to module {self.module_id}"
            )
        return self


class CoverageStatus(StrEnum):
    READY = "ready"
    PENDING = "pending"
    BLOCKED = "blocked"


class SubmoduleCoverageEntry(ReportingModel):
    submodule_id: str
    status: CoverageStatus
    evidence_ids: list[str] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def submodule_is_fixed(self) -> "SubmoduleCoverageEntry":
        resolve_submodule(self.submodule_id)
        return self


class CoverageEntry(ReportingModel):
    module_id: str
    status: CoverageStatus
    evidence_ids: list[str] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)
    submodules: dict[str, SubmoduleCoverageEntry] = Field(default_factory=dict)

    @model_validator(mode="after")
    def submodules_belong_to_module(self) -> "CoverageEntry":
        mismatched = []
        for key, entry in self.submodules.items():
            definition = resolve_submodule(key)
            if entry.submodule_id != key or definition.module_id != self.module_id:
                mismatched.append(key)
        if mismatched:
            raise ValueError(
                f"coverage submodules do not belong to module {self.module_id}: {mismatched}"
            )
        return self


class CoverageMatrix(ReportingModel):
    entries: dict[str, CoverageEntry]

    @model_validator(mode="after")
    def entries_use_fixed_taxonomy(self) -> "CoverageMatrix":
        unknown = sorted(set(self.entries) - set(REPORT_MODULE_IDS))
        mismatched = [key for key, entry in self.entries.items() if entry.module_id != key]
        if unknown or mismatched:
            raise ValueError(
                f"coverage keys must use fixed module ids 2.1-2.5; "
                f"unknown={unknown}, mismatched={mismatched}"
            )
        return self


class ReviewIssue(ReportingModel):
    id: str = Field(default_factory=lambda: f"issue-{uuid4().hex[:12]}", min_length=1)
    module_id: str
    submodule_id: str | None = None
    claim_id: str | None = None
    kind: str = Field(min_length=1)
    message: str = Field(min_length=1)
    severity: Literal["warning", "blocking"]
    round: int = Field(default=0, ge=0)
    status: Literal["open", "resolved"] = "open"
    affected_claim_ids: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    blocking_reason: str | None = None
    resolution_criteria: list[str] = Field(default_factory=list)
    owner_agent_id: str | None = None
    resolved_by_agent_id: str | None = None
    resolution_note: str | None = None
    resolution_evidence_refs: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def blocking_issue_is_actionable_and_resolution_is_auditable(self) -> "ReviewIssue":
        if self.severity == "blocking":
            missing: list[str] = []
            if not self.affected_claim_ids:
                missing.append("affected_claim_ids")
            if not self.evidence_refs:
                missing.append("evidence_refs")
            if not (self.blocking_reason or "").strip():
                missing.append("blocking_reason")
            if not self.resolution_criteria:
                missing.append("resolution_criteria")
            if not (self.owner_agent_id or "").strip():
                missing.append("owner_agent_id")
            if missing:
                raise ValueError(
                    "blocking review issue requires an actionable contract: " + ", ".join(missing)
                )
            if self.status == "resolved":
                resolution_missing: list[str] = []
                if not (self.resolved_by_agent_id or "").strip():
                    resolution_missing.append("resolved_by_agent_id")
                if not (self.resolution_note or "").strip():
                    resolution_missing.append("resolution_note")
                if not self.resolution_evidence_refs:
                    resolution_missing.append("resolution_evidence_refs")
                if resolution_missing:
                    raise ValueError(
                        "resolved blocking review issue requires closure evidence: "
                        + ", ".join(resolution_missing)
                    )
        return self


class OutputArtifact(ReportingModel):
    kind: Literal["module", "review", "report", "run"]
    path: Path
    module_id: str | None = None

"""Typed carriers shared by the power-distribution reporting workflow."""

from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .taxonomy import REPORT_TAXONOMY, resolve_submodule

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


class ClaimKind(StrEnum):
    FACT = "fact"
    CONCLUSION = "conclusion"
    RISK = "risk"
    RECOMMENDATION = "recommendation"


class Claim(ReportingModel):
    id: str = Field(min_length=1)
    module_id: str
    submodule_id: str
    kind: ClaimKind
    text: str = Field(min_length=1)
    evidence_ids: list[str] = Field(default_factory=list)
    skill_ids: list[str] = Field(default_factory=list)
    unverified: bool = False

    @model_validator(mode="after")
    def claim_is_traceable_and_in_taxonomy(self) -> "Claim":
        if self.module_id not in REPORT_TAXONOMY:
            raise ValueError(f"unknown report module: {self.module_id}")
        submodule = resolve_submodule(self.submodule_id)
        if submodule.module_id != self.module_id:
            raise ValueError(
                f"submodule {self.submodule_id} does not belong to module {self.module_id}"
            )
        if not self.evidence_ids and not self.unverified:
            raise ValueError("claim requires evidence_ids or unverified=true")
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


class ModuleTask(ReportingModel):
    id: str = Field(min_length=1)
    module_id: str
    evidence_ids: list[str] = Field(default_factory=list)
    submodule_evidence: dict[str, list[str]] = Field(default_factory=dict)
    missing_submodules: list[str] = Field(default_factory=list)
    skipped_submodules: list[str] = Field(default_factory=list)
    allow_unverified: bool = False
    revision: int = Field(default=0, ge=0)


class ModuleDraft(ReportingModel):
    module_id: str
    markdown: str
    evidence_ids: list[str]
    unverified_items: list[str] = Field(default_factory=list)


class ReviewIssue(ReportingModel):
    module_id: str
    kind: str = Field(min_length=1)
    message: str = Field(min_length=1)
    severity: Literal["warning", "blocking"]


class OutputArtifact(ReportingModel):
    kind: Literal["module", "review", "report", "run"]
    path: Path
    module_id: str | None = None

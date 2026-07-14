from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Annotated, Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

NonBlank = Annotated[str, Field(min_length=1)]
ModuleId = Literal["2.1", "2.2", "2.3", "2.4", "2.5"]


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex[:12]}"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class CoverageStatus(StrEnum):
    READY = "ready"
    PENDING = "pending"
    BLOCKED = "blocked"


class ReviewSeverity(StrEnum):
    INFO = "info"
    WARNING = "warning"
    BLOCKING = "blocking"


class RunStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class ParseStatus(StrEnum):
    PENDING = "pending"
    PARSED = "parsed"
    UNSUPPORTED = "unsupported"
    ERROR = "error"


class SourceLocation(StrictModel):
    file_id: NonBlank
    relative_path: Path
    page: int | None = Field(default=None, ge=1)
    sheet: str | None = None
    cell: str | None = None
    locator: str | None = None

    @field_validator("relative_path")
    @classmethod
    def relative_path_must_be_safe(cls, path: Path) -> Path:
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("relative_path must stay inside the project")
        return path


class ReportRequest(StrictModel):
    request_id: str = Field(default_factory=lambda: new_id("request"))
    task: Literal["write_report"]
    target_modules: list[ModuleId] = Field(default_factory=lambda: ["2.4"])
    execution_requirements: list[NonBlank] = Field(default_factory=list)
    temporary_constraints: list[NonBlank] = Field(default_factory=list)
    allow_pending_evidence: bool = False
    max_revision_rounds: int = Field(default=2, ge=0, le=5)


class ManifestEntry(StrictModel):
    id: NonBlank
    relative_path: Path
    sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    format: NonBlank
    purposes: list[NonBlank] = Field(default_factory=list)
    parse_status: ParseStatus = ParseStatus.PENDING
    error: str | None = None

    @field_validator("relative_path")
    @classmethod
    def path_must_be_relative(cls, path: Path) -> Path:
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("manifest paths must stay inside the project")
        return path


class ProjectManifest(StrictModel):
    project_id: NonBlank
    files: list[ManifestEntry] = Field(default_factory=list)


class ParsedArtifact(StrictModel):
    id: NonBlank
    source: SourceLocation
    content_type: NonBlank
    data: dict[str, Any] = Field(default_factory=dict)


class EvidenceItem(StrictModel):
    id: NonBlank
    fact: NonBlank
    source: SourceLocation
    object: str | None = None
    value: str | None = None
    unit: str | None = None
    observed_at: str | None = None
    confidence: float = Field(default=1.0, ge=0, le=1)
    pending_verification: bool = False


class CoverageEntry(StrictModel):
    module_id: ModuleId
    status: CoverageStatus
    evidence_ids: list[NonBlank] = Field(default_factory=list)
    gaps: list[NonBlank] = Field(default_factory=list)


class CoverageMatrix(StrictModel):
    entries: list[CoverageEntry]


class ModuleTask(StrictModel):
    id: str = Field(default_factory=lambda: new_id("task"))
    module_id: ModuleId
    evidence_ids: list[NonBlank] = Field(default_factory=list)
    knowledge_unit_ids: list[NonBlank] = Field(default_factory=list)
    dependencies: list[NonBlank] = Field(default_factory=list)
    audit_requirements: list[NonBlank] = Field(default_factory=list)


class Claim(StrictModel):
    id: str = Field(default_factory=lambda: new_id("claim"))
    statement: NonBlank
    evidence_ids: list[NonBlank] = Field(default_factory=list)
    risk_level: str | None = None
    pending_verification: bool = False


class ModuleDraft(StrictModel):
    module_id: ModuleId
    claims: list[Claim] = Field(default_factory=list)
    recommendations: list[NonBlank] = Field(default_factory=list)
    pending_verifications: list[NonBlank] = Field(default_factory=list)
    revision: int = Field(default=0, ge=0)


class ReviewIssue(StrictModel):
    id: str = Field(default_factory=lambda: new_id("issue"))
    module_id: ModuleId
    claim_id: str | None = None
    issue_type: NonBlank
    severity: ReviewSeverity
    blocking: bool = False
    message: NonBlank
    revision_request: str | None = None


class OutputArtifact(StrictModel):
    id: str = Field(default_factory=lambda: new_id("output"))
    kind: NonBlank
    relative_path: Path
    source_ids: list[NonBlank] = Field(default_factory=list)

    @field_validator("relative_path")
    @classmethod
    def output_path_must_be_relative(cls, path: Path) -> Path:
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("output path must stay inside the project")
        return path

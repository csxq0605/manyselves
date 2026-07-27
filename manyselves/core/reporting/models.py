"""Typed carriers shared by the power-distribution reporting workflow."""

from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .taxonomy import resolve_submodule

REPORT_MODULE_IDS = ("2.1", "2.2", "2.3", "2.4", "2.5")
REPORT_FINAL_SECTION_IDS = (
    "1.1",
    "1.2",
    "1.3",
    "2.1",
    "2.2",
    "2.3",
    "2.4",
    "2.5",
    "3.1.1",
    "3.1.2",
    "3.1.3",
    "3.1.4",
    "3.2",
    "4.1",
    "4.2",
    "4.3",
    "4.4",
)
REPORT_FINAL_AUDIT_SECTION_IDS = (
    "1.1",
    "1.2",
    "1.3",
    "3.1.1",
    "3.1.2",
    "3.1.3",
    "3.1.4",
    "3.2",
    "4.1",
    "4.2",
    "4.3",
    "4.4",
)
CHIEF_SECTION_RESULT_PART_IDS = {
    "1.1": "assessment_background",
    "1.2": "findings_overview",
    "1.3": "regional_executive_summary",
    "3.1.1": "risk_panorama",
    "3.1.2": "dimension_risk_analysis",
    "3.1.3": "cross_module_analysis",
    "3.1.4": "data_gap_analysis",
    "3.2": "improvement_action_plan",
    "4.1": "new_factory_planning",
    "4.2": "capacity_expansion_plan",
    "4.3": "daily_power_management",
    "4.4": "emergency_compliance_management",
}
CHIEF_RESULT_PART_IDS = tuple(CHIEF_SECTION_RESULT_PART_IDS.values())
ReportOperation = Literal[
    "distill_template_skill",
    "full_report",
    "module_report",
    "aggregate_existing",
    "render_existing",
]


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


SupplementScope = Literal["run", "module", "submodule", "claim", "final_section"]
SupplementStage = Literal[
    "module_authoring",
    "module_review",
    "cross_review",
    "chief_edit",
    "final_review",
]


class UserSupplement(ReportingModel):
    """One scoped, auditable user fact or instruction added to the current run."""

    id: str = Field(
        default_factory=lambda: f"US-{uuid4().hex[:10]}",
        pattern=r"^US-[A-Za-z0-9._-]+$",
        description="Stable current-run supplement id used by supersedes.",
    )
    content: str = Field(
        min_length=1,
        description="Exact user-confirmed fact or instruction without inferred additions.",
    )
    scope: SupplementScope = Field(
        default="run",
        description="Run-wide or explicitly targeted applicability boundary.",
    )
    target_ids: list[str] = Field(
        default_factory=list,
        description="Required module, submodule, Claim, or final-section ids outside run scope.",
    )
    stages: list[SupplementStage] = Field(
        default_factory=lambda: [
            "module_authoring",
            "module_review",
            "cross_review",
            "chief_edit",
            "final_review",
        ],
        description="Only workflow stages allowed to consume this supplement.",
    )
    supersedes: list[str] = Field(
        default_factory=list,
        description="Earlier supplement ids replaced by this one while preserving audit history.",
    )

    @field_validator("content")
    @classmethod
    def content_is_trimmed(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("supplement content must not be blank")
        return value

    @model_validator(mode="after")
    def scope_and_targets_match(self) -> "UserSupplement":
        if len(self.target_ids) != len(set(self.target_ids)):
            raise ValueError("supplement target_ids must be unique")
        if len(self.stages) != len(set(self.stages)):
            raise ValueError("supplement stages must be unique")
        if len(self.supersedes) != len(set(self.supersedes)):
            raise ValueError("supplement supersedes ids must be unique")
        if self.id in self.supersedes:
            raise ValueError("a supplement cannot supersede itself")
        if self.scope == "run" and self.target_ids:
            raise ValueError("run-scoped supplement must not declare target_ids")
        if self.scope != "run" and not self.target_ids:
            raise ValueError(f"{self.scope}-scoped supplement requires target_ids")
        if self.scope == "module":
            unknown = sorted(set(self.target_ids) - set(REPORT_MODULE_IDS))
            if unknown:
                raise ValueError(f"supplement module targets are invalid: {unknown}")
        if self.scope == "submodule":
            for target_id in self.target_ids:
                resolve_submodule(target_id)
        if self.scope == "claim" and any(
            not target_id.startswith("C-") for target_id in self.target_ids
        ):
            raise ValueError("claim-scoped supplement targets must use C-* ids")
        if self.scope == "final_section":
            unknown = sorted(set(self.target_ids) - set(REPORT_FINAL_SECTION_IDS))
            if unknown:
                raise ValueError(f"supplement final-section targets are invalid: {unknown}")
        return self


class ReportRequest(ReportingModel):
    operation: ReportOperation = "full_report"
    instruction: str = Field(min_length=1)
    target_modules: list[str] = Field(default_factory=lambda: list(REPORT_MODULE_IDS))
    source_module_refs: dict[str, Path] | None = None
    source_markdown_ref: Path | None = None
    output_filename: str | None = None
    execution_requirements: list[str] = Field(default_factory=list)
    user_supplements: list[UserSupplement] = Field(
        default_factory=list,
        description="Scoped, stage-bound, superseding current-run user inputs.",
    )
    missing_evidence_policy: Literal["ask", "block", "skip", "draft"] = "ask"
    max_provider_attempts: int = Field(default=80, ge=1, le=1000)
    max_total_tokens: int = Field(default=800_000, ge=1_000)

    @field_validator("target_modules")
    @classmethod
    def target_modules_are_fixed(cls, values: list[str]) -> list[str]:
        unknown = sorted(set(values) - set(REPORT_MODULE_IDS))
        if unknown:
            raise ValueError(f"module ids must be within 2.1-2.5; got {unknown}")
        if len(values) != len(set(values)):
            raise ValueError("target module ids must be unique")
        return values

    @model_validator(mode="after")
    def operation_inputs_are_complete(self) -> "ReportRequest":
        supplement_ids = [item.id for item in self.user_supplements]
        if len(supplement_ids) != len(set(supplement_ids)):
            raise ValueError("user supplement ids must be unique")
        known_supplements = set(supplement_ids)
        unknown_superseded = sorted(
            {superseded for item in self.user_supplements for superseded in item.supersedes}
            - known_supplements
        )
        if unknown_superseded:
            raise ValueError(f"supplements supersede unknown ids: {unknown_superseded}")
        requested = set(self.target_modules)
        all_modules = set(REPORT_MODULE_IDS)
        if self.operation == "distill_template_skill" and requested:
            raise ValueError("distill_template_skill does not accept target modules")
        if self.operation == "full_report" and requested != all_modules:
            raise ValueError("full_report requires exactly modules 2.1-2.5")
        if self.operation == "module_report" and not requested:
            raise ValueError("module_report requires at least one target module")
        if self.operation == "module_report" and requested == all_modules:
            raise ValueError("module_report must be a proper subset of modules 2.1-2.5")
        existing_module_operations = {"aggregate_existing"}
        if self.operation in existing_module_operations and requested != all_modules:
            raise ValueError(f"{self.operation} requires exactly modules 2.1-2.5")
        if self.operation == "render_existing" and self.source_markdown_ref is None:
            raise ValueError("render_existing requires source_markdown_ref")
        if self.operation != "render_existing" and self.source_markdown_ref is not None:
            raise ValueError("source_markdown_ref is only valid for render_existing")
        if self.operation not in existing_module_operations and self.source_module_refs is not None:
            raise ValueError("source_module_refs is only valid for aggregate_existing")
        if self.source_module_refs is not None:
            if set(self.source_module_refs) != all_modules:
                raise ValueError("source_module_refs requires exactly modules 2.1-2.5")
            for module_id, path in self.source_module_refs.items():
                if path.is_absolute() or ".." in path.parts:
                    raise ValueError(
                        f"source module {module_id} must stay inside the project workspace"
                    )
                if path.suffix.casefold() not in {".md", ".markdown", ".json"}:
                    raise ValueError(
                        f"source module {module_id} must be Markdown or a structured module JSON"
                    )
        if self.source_markdown_ref is not None:
            if self.source_markdown_ref.is_absolute() or ".." in self.source_markdown_ref.parts:
                raise ValueError("source_markdown_ref must stay inside the project workspace")
            if self.source_markdown_ref.suffix.casefold() not in {".md", ".markdown"}:
                raise ValueError("source_markdown_ref must be a Markdown file")
        if self.output_filename is not None:
            output = Path(self.output_filename)
            if output.name != self.output_filename or output.suffix.casefold() != ".docx":
                raise ValueError("output_filename must be one DOCX filename")
        return self


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
    decision_note: str | None = None
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
    user_supplements: list[UserSupplement] = Field(
        default_factory=list,
        description="Scoped, stage-bound additions supplied while this revision run is resumed.",
    )
    promote_to_skill: bool = False
    promote_skill_id: str | None = None
    max_provider_attempts: int = Field(default=40, ge=1, le=1000)
    max_total_tokens: int = Field(default=400_000, ge=1_000)

    @model_validator(mode="after")
    def targets_stay_in_selected_modules(self) -> "RevisionRequest":
        supplement_ids = [item.id for item in self.user_supplements]
        if len(supplement_ids) != len(set(supplement_ids)):
            raise ValueError("revision user supplement ids must be unique")
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


class OutputArtifact(ReportingModel):
    kind: Literal["module", "review", "report", "run", "skill"]
    path: Path
    module_id: str | None = None

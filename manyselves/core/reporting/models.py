"""Typed carriers shared by the power-distribution reporting workflow."""

import re
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from manyselves.capabilities.distribution_reporting.domain.taxonomy import resolve_submodule

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
    "3.2",
    "4",
)
REPORT_FINAL_AUDIT_SECTION_IDS = (
    "1.1",
    "1.2",
    "1.3",
    "3.1.1",
    "3.1.2",
    "3.1.3",
    "3.2",
    "4",
)
# Semantic summary/conclusion audit scope.  Chapter 2 is owned by the five
# module reviews and Chapter 4 is an optional Inputs topic, so neither is a
# final summary/conclusion finding target.
FINAL_SUMMARY_CONCLUSION_AUDIT_SECTION_IDS = (
    "1.1",
    "1.2",
    "1.3",
    "3.1.1",
    "3.1.2",
    "3.1.3",
    "3.2",
)
CHIEF_SECTION_RESULT_PART_IDS = {
    "1.1": "assessment_background",
    "1.2": "findings_overview",
    "1.3": "regional_executive_summary",
    "3.1.1": "risk_panorama",
    "3.1.2": "dimension_risk_analysis",
    "3.1.3": "data_gap_analysis",
    "3.2": "improvement_action_plan",
    "4": "special_topic_analysis",
}
CHIEF_RESULT_PART_IDS = tuple(CHIEF_SECTION_RESULT_PART_IDS.values())

# Chief/Final are intentionally partitioned by report chapter.  These are
# lane-local scopes, not a new report-wide schema: a lane receives only the
# section bodies belonging to its chapter and the reducer owns assembly.
CHAPTER1_SECTION_IDS = ("1.1", "1.2", "1.3")
CHAPTER3_SECTION_IDS = ("3.1.1", "3.1.2", "3.1.3", "3.2")
CHAPTER_STATIC_SECTION_IDS = {
    "1": CHAPTER1_SECTION_IDS,
    "3": CHAPTER3_SECTION_IDS,
}
CHAPTER_IDS = ("1", "3", "4")


def chapter_section_ids(
    chapter_id: str,
    special_topic_plan: "SpecialTopicPlan | None" = None,
) -> tuple[str, ...]:
    """Return the exact active section ids for one Chief/Final lane.

    Chapter 4 is dynamic and exists only when the Inputs-derived special-topic
    plan is present.  Keeping this check in the shared model layer prevents a
    workflow reducer from accidentally creating a phantom Chapter 4 lane.
    """

    if chapter_id in CHAPTER_STATIC_SECTION_IDS:
        return tuple(CHAPTER_STATIC_SECTION_IDS[chapter_id])
    if chapter_id == "4":
        if special_topic_plan is None:
            raise ValueError("Chapter 4 lane requires an active special_topic_plan")
        return tuple(section.section_id for section in special_topic_plan.sections)
    raise ValueError(f"unsupported Chief/Final chapter lane: {chapter_id}")
ReportOperation = Literal[
    "distill_template_skill",
    "full_report",
    "module_report",
    "aggregate_existing",
    "render_existing",
]
CostControlMode = Literal["observe", "warn", "pause_at_boundary"]


class ReportingModel(BaseModel):
    """Strict base model for persisted workflow state."""

    model_config = ConfigDict(extra="forbid")


def __getattr__(name: str):
    """Lazily expose the Cross decision carrier without an import cycle."""

    if name == "CrossDecisionPack":
        from . import agentic_models

        return getattr(agentic_models, name)
    if name == "CrossDecisionPackView":
        from . import input_contracts

        return input_contracts.CrossDecisionPackView
    raise AttributeError(name)


class SpecialTopicSectionRequirement(ReportingModel):
    """One runtime-numbered Chapter 4 subsection requested by the user input."""

    section_id: str = Field(
        pattern=r"^4\.[1-9][0-9]*$",
        description="Runtime-assigned sequential Chapter 4 subsection id.",
    )
    title: str = Field(
        min_length=1,
        max_length=200,
        description="Exact visible subsection title parsed from the Inputs plan.",
    )
    requirement: str = Field(
        min_length=1,
        max_length=20_000,
        description="User-authored brief writing requirement for this subsection.",
    )


class SpecialTopicPlan(ReportingModel):
    """Immutable provenance and writing requirements for the dynamic fourth chapter."""

    source_ref: Path = Field(
        description="Relative path of the one standalone Inputs Markdown source."
    )
    source_sha256: str = Field(
        pattern=r"^[0-9a-f]{64}$",
        description="Immutable SHA-256 of the source Markdown bytes.",
    )
    sections: list[SpecialTopicSectionRequirement] = Field(
        min_length=1,
        max_length=30,
        description="Ordered dynamic Chapter 4 subsection titles and requirements.",
    )

    @field_validator("source_ref", mode="before")
    @classmethod
    def source_must_be_an_inputs_markdown(cls, value: Any) -> Any:
        path = Path(value)
        if (
            path.is_absolute()
            or not path.parts
            or path.parts[0] != "Inputs"
            or path.suffix.casefold() != ".md"
            or ".." in path.parts
        ):
            raise ValueError("special-topic source must be a relative Markdown path under Inputs")
        return path.as_posix()

    @model_validator(mode="after")
    def section_ids_and_titles_are_unique_and_ordered(self) -> "SpecialTopicPlan":
        expected_ids = [f"4.{index}" for index in range(1, len(self.sections) + 1)]
        actual_ids = [section.section_id for section in self.sections]
        if actual_ids != expected_ids:
            raise ValueError(
                "special-topic section ids must be sequential in source order: "
                f"expected={expected_ids}, actual={actual_ids}"
            )
        titles = [section.title for section in self.sections]
        if len(titles) != len(set(titles)):
            raise ValueError("special-topic section titles must be unique")
        return self

    def validate_analysis(
        self,
        markdown: str,
        *,
        allow_chapter_heading: bool = False,
    ) -> None:
        """Validate planned Chapter 4 headings while allowing nested structure.

        The Inputs plan owns the ordered ``### 4.n`` section boundaries.  A
        Chief may add numbered descendants such as ``#### 4.1.1`` inside the
        matching planned section, but may not create another top-level 4.x
        section or place a descendant under a different parent.
        """

        parsed = [
            (len(match.group(1)), match.group(2).strip(), line_number)
            for line_number, line in enumerate(markdown.splitlines())
            if (match := re.match(r"^(#{1,6})\s+(.+?)\s*$", line))
        ]
        expected = [
            (3, f"{section.section_id} {section.title}")
            for section in self.sections
        ]
        numbered: list[tuple[int, str, str, int]] = []
        chapter_headings: list[tuple[int, str, int]] = []
        for level, title, line_number in parsed:
            match = re.match(r"^(4(?:\.\d+)+)\.?\s+(.+?)\s*$", title)
            if match:
                numbered.append((level, match.group(1), title, line_number))
                continue
            if re.match(r"^4\.?\s+", title):
                chapter_headings.append((level, title, line_number))
                continue
            if re.match(r"^\d+(?:\.\d+)*\.?\s+", title):
                raise ValueError(
                    "special_topic_analysis contains a numbered heading outside Chapter 4: "
                    f"{title}"
                )

        if chapter_headings and not allow_chapter_heading:
            raise ValueError(
                "special_topic_analysis must not contain its runtime-owned Chapter 4 heading"
            )
        if len(chapter_headings) > 1:
            raise ValueError("special_topic_analysis contains duplicate Chapter 4 headings")

        top_level = [
            (level, title, line_number)
            for level, section_id, title, line_number in numbered
            if section_id.count(".") == 1
        ]
        if [(level, title) for level, title, _ in top_level] != expected:
            raise ValueError(
                "special_topic_analysis headings must exactly match the Inputs plan: "
                f"expected={expected}, actual="
                f"{[(level, title) for level, title, _ in top_level]}"
            )

        expected_ids = {section.section_id for section in self.sections}
        seen_heading_ids: set[str] = set()
        active_parent: str | None = None
        for level, section_id, title, _line_number in numbered:
            parts = section_id.split(".")
            if len(parts) == 2:
                active_parent = section_id
                seen_heading_ids.add(section_id)
                continue
            parent = ".".join(parts[:2])
            immediate_parent = ".".join(parts[:-1])
            expected_level = len(parts) + 1
            if (
                parent not in expected_ids
                or active_parent != parent
                or immediate_parent not in seen_heading_ids
            ):
                raise ValueError(
                    "special_topic_analysis nested heading is outside its planned parent: "
                    f"{title}"
                )
            if level != expected_level:
                raise ValueError(
                    "special_topic_analysis nested heading has the wrong Markdown level: "
                    f"expected={expected_level}, actual={level}, heading={title}"
                )
            if section_id in seen_heading_ids:
                raise ValueError(
                    f"special_topic_analysis contains duplicate nested heading id: {section_id}"
                )
            seen_heading_ids.add(section_id)

        lines = markdown.splitlines()
        for index, (_level, title, start) in enumerate(top_level):
            end = (
                top_level[index + 1][2]
                if index + 1 < len(top_level)
                else len(lines)
            )
            body = "\n".join(lines[start + 1 : end]).strip()
            if len(re.sub(r"\s+", "", body)) < 20:
                raise ValueError(f"special-topic section has no substantive body: {title}")


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
    missing_evidence_policy: Literal["ask", "block", "skip", "draft"] = "draft"
    cost_control_mode: CostControlMode = "observe"
    max_provider_attempts: int = Field(default=80, ge=1, le=1000)
    max_total_tokens: int = Field(default=800_000, ge=1_000)
    preparation_mode: Literal[
        "serial", "deterministic_workers"
    ] = "deterministic_workers"
    preparation_concurrency: int = Field(default=4, ge=1, le=16)

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
    cost_control_mode: CostControlMode = "observe"
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
    snapshot_ref: Path | None = Field(
        default=None,
        description="Run-frozen source bytes used for parsing; path remains logical provenance.",
    )
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
    source_image_id: str | None = Field(
        default=None,
        description="Original workbook DISPIMG/OOXML image key retained for provenance.",
    )
    primary_evidence_id: str | None = Field(
        default=None,
        description=(
            "Explicit EvidenceItem owner used for photo caption and placement when "
            "one source image is associated with multiple evidence facts."
        ),
    )


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

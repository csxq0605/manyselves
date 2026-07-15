from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .models import CoverageMatrix, EvidenceItem, PhotoAsset


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SourceKind(StrEnum):
    PROJECT_EVIDENCE = "project_evidence"
    LOCAL_REFERENCE = "local_reference"
    WEB = "web"


SOURCE_PREFIX = {
    SourceKind.PROJECT_EVIDENCE: "E-",
    SourceKind.LOCAL_REFERENCE: "R-",
    SourceKind.WEB: "W-",
}


class TaskEnvelope(StrictModel):
    task_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    agent_id: str = Field(min_length=1)
    objective: str = Field(min_length=1)
    input_refs: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    allowed_outputs: list[str] = Field(default_factory=list)
    revision: int = Field(default=0, ge=0)
    prior_result_ref: str | None = None
    issue_refs: list[str] = Field(default_factory=list)


class PreparationResult(StrictModel):
    input_refs: list[str] = Field(min_length=1)
    evidence_items: list[EvidenceItem]
    coverage: CoverageMatrix
    photo_assets: list[PhotoAsset] = Field(default_factory=list)


class SourceRecord(StrictModel):
    id: str = Field(min_length=3)
    kind: SourceKind
    title: str = Field(min_length=1)
    locator: str = Field(min_length=1)
    publisher: str | None = None
    published_at: str | None = None
    accessed_at: str | None = None
    scope_note: str | None = None
    content_sha256: str | None = None

    @model_validator(mode="after")
    def id_matches_kind(self) -> "SourceRecord":
        if not self.id.startswith(SOURCE_PREFIX[self.kind]):
            raise ValueError("source id prefix does not match kind")
        return self


class ResearchNote(StrictModel):
    id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    question: str = Field(min_length=1)
    synthesis: str = Field(min_length=1)
    source_ids: list[str] = Field(min_length=1)
    applicability: str = Field(min_length=1)


class ClaimRecord(StrictModel):
    id: str = Field(pattern=r"^C-")
    module_id: Literal["2.4"]
    text: str = Field(min_length=1)
    claim_type: Literal[
        "project_fact", "technical_interpretation", "risk_judgment", "recommendation"
    ]
    source_ids: list[str] = Field(default_factory=list)
    confidence: Annotated[float, Field(ge=0.0, le=1.0)] = 1.0
    footnote_required: bool = True
    unresolved: bool = False

    @model_validator(mode="after")
    def project_fact_uses_evidence(self) -> "ClaimRecord":
        if self.claim_type == "project_fact" and not any(
            source_id.startswith("E-") for source_id in self.source_ids
        ):
            raise ValueError("project_fact requires at least one E-* source")
        return self


class ModuleSubmission(StrictModel):
    kind: Literal["module_submission"] = "module_submission"
    module_id: Literal["2.4"]
    markdown: str = Field(min_length=1)
    claims: list[ClaimRecord]
    source_ids: list[str]
    unresolved_questions: list[str]
    revision: int = Field(ge=0)


class PlanSubmission(StrictModel):
    kind: Literal["plan_submission"] = "plan_submission"
    module_tasks: list[TaskEnvelope] = Field(min_length=1)
    rationale: str = Field(min_length=1)


class AuditSubmission(StrictModel):
    kind: Literal["audit_submission"] = "audit_submission"
    module_id: Literal["2.4"]
    approved: bool
    issues: list[dict]
    checked_claim_ids: list[str]


Submission = Annotated[
    ModuleSubmission | PlanSubmission | AuditSubmission,
    Field(discriminator="kind"),
]


class AgentRunStatus(StrEnum):
    COMPLETED = "completed"
    BLOCKED = "blocked"
    INCOMPLETE = "incomplete"
    FAILED = "failed"


class AgentResult(StrictModel):
    task_id: str
    run_id: str
    agent_id: str
    session_id: str
    status: AgentRunStatus
    payload: Submission | None = None
    raw_output: str = ""
    reason: str | None = None

    @model_validator(mode="after")
    def completed_has_payload(self) -> "AgentResult":
        if self.status == AgentRunStatus.COMPLETED and self.payload is None:
            raise ValueError("completed result requires payload")
        return self


class CitationEntry(StrictModel):
    marker: int = Field(ge=1)
    claim_id: str
    source_ids: list[str] = Field(min_length=1)


class CitationPlan(StrictModel):
    entries: list[CitationEntry]
    evidence_index_markdown: str

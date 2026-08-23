"""Reporting REST DTOs translated into the existing strict core models."""

from pathlib import Path
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from manyselves.capabilities.distribution_reporting.runtime.models.reporting import (
    ReportRequest,
    RevisionRequest,
    UserSupplement,
)


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class ReportingStartRequest(_Strict):
    operation: Literal[
        "distill_template_skill", "full_report", "module_report",
        "aggregate_existing", "render_existing",
    ] = "full_report"
    instruction: str = Field(min_length=1)
    target_modules: list[str] | None = Field(default=None, alias="targetModules")
    source_module_refs: dict[str, Path] | None = Field(default=None, alias="sourceModuleRefs")
    source_markdown_ref: Path | None = Field(default=None, alias="sourceMarkdownRef")
    output_filename: str | None = Field(default=None, alias="outputFilename")
    execution_requirements: list[str] = Field(default_factory=list, alias="executionRequirements")
    user_supplements: list[UserSupplement] = Field(default_factory=list, alias="userSupplements")
    missing_evidence_policy: Literal["ask", "block", "skip", "draft"] = Field(
        default="draft", alias="missingEvidencePolicy"
    )
    max_provider_attempts: int = Field(default=80, alias="maxProviderAttempts")
    max_total_tokens: int = Field(default=800_000, alias="maxTotalTokens")

    def to_core(self) -> ReportRequest:
        values = self.model_dump()
        if values["target_modules"] is None:
            values.pop("target_modules")
        return ReportRequest.model_validate(values)


class ReportingResumeRequest(_Strict):
    max_provider_attempts: int | None = Field(default=None, alias="maxProviderAttempts")
    max_total_tokens: int | None = Field(default=None, alias="maxTotalTokens")
    supplements: list[UserSupplement] = Field(default_factory=list)


class ReportingDecisionRequest(_Strict):
    action: Literal["supplement", "draft", "skip", "stop"]
    supplements: list[UserSupplement] = Field(default_factory=list)


class ReportingRevisionRequest(_Strict):
    baseline_version_id: str = Field(alias="baselineVersionId")
    feedback: str
    target_module_ids: list[Literal["2.1", "2.2", "2.3", "2.4", "2.5"]] = Field(alias="targetModuleIds")
    target_submodule_ids: list[str] = Field(default_factory=list, alias="targetSubmoduleIds")
    target_claim_ids: list[str] = Field(default_factory=list, alias="targetClaimIds")
    user_supplements: list[UserSupplement] = Field(default_factory=list, alias="userSupplements")
    promote_to_skill: bool = Field(default=False, alias="promoteToSkill")
    promote_skill_id: str | None = Field(default=None, alias="promoteSkillId")
    max_provider_attempts: int = Field(default=40, alias="maxProviderAttempts")
    max_total_tokens: int = Field(default=400_000, alias="maxTotalTokens")

    def to_core(self) -> RevisionRequest:
        return RevisionRequest.model_validate(self.model_dump())


class ReportingAcceptedResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    command_id: UUID = Field(alias="commandId")
    status: Literal["accepted"] = "accepted"
    run_id: str = Field(alias="runId")
    task_id: str | None = Field(default=None, alias="taskId")


class ReportingListResponse(BaseModel):
    runs: list[dict[str, Any]]


class ReportingSnapshotResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    run: dict[str, Any]
    state: dict[str, Any]
    waiting_input: list[dict[str, Any]] = Field(alias="waitingInput")
    checkpoint: dict[str, Any]
    evidence: dict[str, Any]
    revision: dict[str, Any]
    outputs: list[dict[str, Any]]

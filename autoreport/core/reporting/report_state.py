"""Audited immutable input state for final report rendering."""

from pydantic import Field, model_validator

from .editing.chief import ChiefEditor, ChiefEditorResult
from .models import (
    CoverageMatrix,
    EvidenceItem,
    ModuleDraft,
    PhotoAsset,
    ProjectManifest,
    ReportingModel,
    ReportRequest,
    ReviewIssue,
)
from .workers.orchestrator import ModuleExecution


class ReportState(ReportingModel):
    title: str = Field(min_length=1)
    request: ReportRequest
    manifest: ProjectManifest
    coverage: CoverageMatrix
    evidence_items: list[EvidenceItem]
    photo_assets: list[PhotoAsset]
    module_drafts: list[ModuleDraft]
    review_issues: list[ReviewIssue]
    editorial: ChiefEditorResult
    rule_version: str = "v2-handoff-2026-05-29"
    skill_versions: list[str] = Field(default_factory=list)
    model_versions: dict[str, str] = Field(
        default_factory=lambda: {"drafting": "deterministic-v1", "editing": "chief-v1"}
    )
    review_history: list[ReviewIssue] = Field(default_factory=list)
    module_executions: list[ModuleExecution] = Field(default_factory=list)

    @model_validator(mode="after")
    def state_is_approved_and_traceable(self) -> "ReportState":
        unapproved = [draft.module_id for draft in self.module_drafts if not draft.approved]
        if unapproved:
            raise ValueError(f"all module drafts must be approved; got {unapproved}")
        blocking = [issue.kind for issue in self.review_issues if issue.severity == "blocking"]
        if blocking:
            raise ValueError(f"blocking review issues remain: {blocking}")
        known_evidence = {item.id for item in self.evidence_items}
        claim_ids = {claim.id for draft in self.module_drafts for claim in draft.claims}
        if set(self.editorial.protected_claim_ids) != claim_ids:
            raise ValueError("editorial protected claim set does not match module drafts")
        for draft in self.module_drafts:
            unknown_draft = sorted(set(draft.evidence_ids) - known_evidence)
            if unknown_draft:
                raise ValueError(
                    f"draft {draft.module_id} references unknown evidence: {unknown_draft}"
                )
            for claim in draft.claims:
                unknown_claim = sorted(set(claim.evidence_ids) - known_evidence)
                if unknown_claim:
                    raise ValueError(
                        f"claim {claim.id} references unknown evidence: {unknown_claim}"
                    )
        return self


def build_report_state(
    *,
    title: str,
    request: ReportRequest,
    manifest: ProjectManifest,
    coverage: CoverageMatrix,
    evidence_items: list[EvidenceItem],
    photo_assets: list[PhotoAsset],
    module_drafts: list[ModuleDraft],
    review_issues: list[ReviewIssue],
    module_executions: list[ModuleExecution] | None = None,
) -> ReportState:
    editorial = ChiefEditor().compile(
        module_drafts,
        [
            issue
            for issue in review_issues
            if issue.kind in {"metric_conflict", "duplicate_claim", "action_conflict"}
        ],
    )
    skill_versions = sorted(
        {
            skill_id
            for draft in module_drafts
            for claim in draft.claims
            for skill_id in claim.skill_ids
        }
    )
    return ReportState(
        title=title,
        request=request,
        manifest=manifest,
        coverage=coverage,
        evidence_items=evidence_items,
        photo_assets=photo_assets,
        module_drafts=module_drafts,
        review_issues=review_issues,
        editorial=editorial,
        skill_versions=skill_versions,
        review_history=review_issues,
        module_executions=module_executions or [],
    )

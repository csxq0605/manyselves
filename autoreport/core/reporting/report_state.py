"""Audited immutable input state for final report rendering."""

from pydantic import Field, model_validator

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


class ReportState(ReportingModel):
    title: str = Field(min_length=1)
    request: ReportRequest
    manifest: ProjectManifest
    coverage: CoverageMatrix
    evidence_items: list[EvidenceItem]
    photo_assets: list[PhotoAsset]
    module_drafts: list[ModuleDraft]
    review_issues: list[ReviewIssue]

    @model_validator(mode="after")
    def state_is_approved_and_traceable(self) -> "ReportState":
        unapproved = [draft.module_id for draft in self.module_drafts if not draft.approved]
        if unapproved:
            raise ValueError(f"all module drafts must be approved; got {unapproved}")
        blocking = [issue.kind for issue in self.review_issues if issue.severity == "blocking"]
        if blocking:
            raise ValueError(f"blocking review issues remain: {blocking}")
        known_evidence = {item.id for item in self.evidence_items}
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
) -> ReportState:
    return ReportState(
        title=title,
        request=request,
        manifest=manifest,
        coverage=coverage,
        evidence_items=evidence_items,
        photo_assets=photo_assets,
        module_drafts=module_drafts,
        review_issues=review_issues,
    )

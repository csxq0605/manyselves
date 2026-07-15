from pathlib import Path

import pytest
from pydantic import ValidationError

from autoreport.core.reporting.coverage import evaluate_coverage
from autoreport.core.reporting.models import (
    Claim,
    ClaimKind,
    EvidenceItem,
    ModuleDraft,
    ProjectManifest,
    ReportRequest,
    ReviewIssue,
    SourceLocation,
)
from autoreport.core.reporting.report_state import build_report_state


def _components(approved: bool = True):
    request = ReportRequest(
        instruction="生成 2.4",
        target_modules=["2.4"],
        missing_evidence_policy="skip",
    )
    evidence = EvidenceItem(
        id="ev-1",
        subject="车间配电房/1A2",
        fact="电缆状态=NG",
        source=SourceLocation(file_id="file-1", path=Path("Inputs/S4-4.xlsx")),
        module_id="2.4",
        submodule_id="2.4.2.5",
    )
    claim = Claim(
        id="claim-1",
        module_id="2.4",
        submodule_id="2.4.2.5",
        kind=ClaimKind.CONCLUSION,
        text="电缆状态异常。",
        evidence_ids=["ev-1"],
        skill_ids=["pds.module24.installation@1.0.0"],
    )
    draft = ModuleDraft(
        module_id="2.4",
        markdown="draft",
        evidence_ids=["ev-1"],
        claims=[claim],
        approved=approved,
    )
    coverage = evaluate_coverage(request, [evidence])
    return request, evidence, draft, coverage


def test_report_state_accepts_only_audited_approved_drafts() -> None:
    request, evidence, draft, coverage = _components()

    state = build_report_state(
        title="配电安全评估报告",
        request=request,
        manifest=ProjectManifest(),
        coverage=coverage,
        evidence_items=[evidence],
        photo_assets=[],
        module_drafts=[draft],
        review_issues=[],
    )

    assert state.module_drafts[0].approved is True
    assert state.evidence_items[0].id == "ev-1"
    assert state.editorial.protected_claim_ids == ["claim-1"]
    assert state.rule_version == "v2-handoff-2026-05-29"
    assert state.skill_versions == ["pds.module24.installation@1.0.0"]


def test_report_state_rejects_unapproved_draft_or_blocking_issue() -> None:
    request, evidence, unapproved, coverage = _components(approved=False)
    with pytest.raises(ValidationError, match="approved"):
        build_report_state(
            title="配电安全评估报告",
            request=request,
            manifest=ProjectManifest(),
            coverage=coverage,
            evidence_items=[evidence],
            photo_assets=[],
            module_drafts=[unapproved],
            review_issues=[],
        )

    request, evidence, approved, coverage = _components(approved=True)
    with pytest.raises(ValidationError, match="blocking"):
        build_report_state(
            title="配电安全评估报告",
            request=request,
            manifest=ProjectManifest(),
            coverage=coverage,
            evidence_items=[evidence],
            photo_assets=[],
            module_drafts=[approved],
            review_issues=[
                ReviewIssue(
                    module_id="2.4",
                    kind="test",
                    message="阻塞",
                    severity="blocking",
                )
            ],
        )

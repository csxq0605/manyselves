from pathlib import Path

from autoreport.core.reporting.models import (
    Claim,
    ClaimKind,
    EvidenceItem,
    ModuleDraft,
    SourceLocation,
)
from autoreport.core.reporting.review.auditor import audit_draft
from autoreport.core.reporting.skills.resolver import SkillResolver


def _evidence(
    *,
    evidence_id: str = "ev-1",
    submodule_id: str = "2.4.1.1",
    fact: str = "计算负荷率=96.99%",
    value: float | None = 96.992,
    unit: str | None = "%",
    needs_confirmation: bool = False,
    photo_refs: list[str] | None = None,
) -> EvidenceItem:
    return EvidenceItem(
        id=evidence_id,
        subject="车间配电房/2A2",
        fact=fact,
        source=SourceLocation(
            file_id="file-s44",
            path=Path("Inputs/S4-4诊断工作用表.xlsx"),
            sheet="低配评估详情",
            cell="C6:E6",
        ),
        module_id="2.4",
        submodule_id=submodule_id,
        value=value,
        unit=unit,
        needs_confirmation=needs_confirmation,
        photo_refs=photo_refs or [],
    )


def _claim(
    *,
    claim_id: str = "claim-1",
    submodule_id: str = "2.4.1.1",
    text: str = "负荷率为96.99%，未达到100%。",
    evidence_ids: list[str] | None = None,
    skill_id: str = "pds.module24.configuration@1.0.0",
) -> Claim:
    return Claim(
        id=claim_id,
        module_id="2.4",
        submodule_id=submodule_id,
        kind=ClaimKind.CONCLUSION,
        text=text,
        evidence_ids=evidence_ids or ["ev-1"],
        skill_ids=[skill_id],
    )


def _draft(claim: Claim) -> ModuleDraft:
    return ModuleDraft(
        module_id="2.4",
        markdown="draft",
        evidence_ids=claim.evidence_ids,
        claims=[claim],
    )


def test_auditor_blocks_unknown_and_wrong_submodule_evidence() -> None:
    resolver = SkillResolver.packaged()
    unknown = audit_draft(_draft(_claim(evidence_ids=["ev-missing"])), [], resolver)
    wrong = audit_draft(
        _draft(_claim(submodule_id="2.4.2.2", skill_id="pds.module24.installation@1.0.0")),
        [_evidence()],
        resolver,
    )

    assert any(
        issue.kind == "unknown_evidence" and issue.severity == "blocking" for issue in unknown
    )
    assert any(issue.kind == "wrong_submodule" and issue.severity == "blocking" for issue in wrong)


def test_auditor_blocks_false_overload_claim_for_96_99_percent() -> None:
    issues = audit_draft(
        _draft(_claim(text="车间配电房2A2当前已过载运行。")),
        [_evidence()],
        SkillResolver.packaged(),
    )

    assert any(
        issue.kind == "threshold_misuse" and issue.severity == "blocking" for issue in issues
    )


def test_auditor_applies_false_overload_guard_to_module_21() -> None:
    evidence = _evidence().model_copy(update={"module_id": "2.1", "submodule_id": "2.1.1"})
    claim = _claim(text="车间配电房2A2当前已过载运行。").model_copy(
        update={
            "module_id": "2.1",
            "submodule_id": "2.1.1",
            "skill_ids": ["pds.module21.architecture@1.0.0"],
        }
    )
    draft = ModuleDraft(
        module_id="2.1",
        markdown="draft",
        evidence_ids=[evidence.id],
        claims=[claim],
    )

    issues = audit_draft(draft, [evidence], SkillResolver.packaged())

    assert any(issue.kind == "threshold_misuse" for issue in issues)


def test_auditor_records_ng_without_photo_as_warning() -> None:
    evidence = _evidence(
        submodule_id="2.4.2.5",
        fact="电缆状态=NG",
        value=None,
        unit=None,
        needs_confirmation=True,
    )
    claim = _claim(
        submodule_id="2.4.2.5",
        text="该异常缺少配套照片，需复核。",
        skill_id="pds.module24.installation@1.0.0",
    )

    issues = audit_draft(_draft(claim), [evidence], SkillResolver.packaged())

    assert any(issue.kind == "missing_photo" and issue.severity == "warning" for issue in issues)
    assert not any(issue.severity == "blocking" for issue in issues)


def test_auditor_deduplicates_missing_photo_by_evidence() -> None:
    evidence = _evidence(
        submodule_id="2.4.2.5",
        fact="电缆状态=NG",
        value=None,
        unit=None,
        needs_confirmation=True,
    )
    claims = [
        _claim(
            claim_id=f"claim-{index}",
            submodule_id="2.4.2.5",
            text="该异常缺少配套照片，需复核。",
            skill_id="pds.module24.installation@1.0.0",
        )
        for index in range(2)
    ]
    draft = ModuleDraft(
        module_id="2.4",
        markdown="draft",
        evidence_ids=["ev-1"],
        claims=claims,
    )

    issues = audit_draft(draft, [evidence], SkillResolver.packaged())

    assert len([issue for issue in issues if issue.kind == "missing_photo"]) == 1

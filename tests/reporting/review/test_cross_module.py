from pathlib import Path

from autoreport.core.reporting.models import (
    Claim,
    ClaimKind,
    EvidenceItem,
    ModuleDraft,
    SourceLocation,
)
from autoreport.core.reporting.review.cross_module import cross_module_review


def _evidence(
    identifier: str,
    module_id: str,
    submodule_id: str,
    value: float,
    *,
    cell: str = "C4:E4",
) -> EvidenceItem:
    return EvidenceItem(
        id=identifier,
        subject="1#变压器/负荷率",
        fact=f"计算负荷率={value}%",
        source=SourceLocation(
            file_id="file",
            path=Path("Inputs/source.xlsx"),
            sheet="数据",
            cell=cell,
        ),
        value=value,
        unit="%",
        module_id=module_id,
        submodule_id=submodule_id,
    )


def _draft(evidence: EvidenceItem, text: str) -> ModuleDraft:
    claim = Claim(
        id=f"claim-{evidence.id}",
        module_id=evidence.module_id,
        submodule_id=evidence.submodule_id,
        kind=ClaimKind.FACT,
        text=text,
        evidence_ids=[evidence.id],
        skill_ids=[f"pds.{evidence.module_id}@1.0.0"],
    )
    return ModuleDraft(
        module_id=evidence.module_id,
        markdown=text,
        evidence_ids=[evidence.id],
        claims=[claim],
    )


def test_cross_module_review_detects_conflicting_metrics() -> None:
    first = _evidence("ev-21", "2.1", "2.1.1", 96.99)
    second = _evidence("ev-24", "2.4", "2.4.1.1", 110.0)
    drafts = [_draft(first, "同一设备需要复核。"), _draft(second, "同一设备需要复核。")]

    issues = cross_module_review(drafts, [first, second])

    assert any(issue.kind == "metric_conflict" and issue.severity == "blocking" for issue in issues)
    assert not any(issue.kind == "duplicate_claim" for issue in issues)
    assert {issue.module_id for issue in issues} <= {"2.1", "2.4"}


def test_cross_module_review_warns_for_duplicate_facts_from_distinct_sources() -> None:
    first = _evidence("ev-21", "2.1", "2.1.1", 96.99, cell="C4:E4")
    second = _evidence("ev-24", "2.4", "2.4.1.1", 96.99, cell="C5:E5")

    issues = cross_module_review(
        [_draft(first, "同一设备需要复核。"), _draft(second, "同一设备需要复核。")],
        [first, second],
    )

    assert any(issue.kind == "duplicate_claim" and issue.severity == "warning" for issue in issues)


def test_cross_module_review_blocks_opposing_actions_for_same_subject() -> None:
    first = _evidence("ev-21", "2.1", "2.1.1", 96.99)
    second = _evidence("ev-24", "2.4", "2.4.1.1", 96.99)
    first_draft = _draft(first, "立即停运1#变压器。")
    second_draft = _draft(second, "继续运行1#变压器。")
    first_draft.claims[0].kind = ClaimKind.RECOMMENDATION
    second_draft.claims[0].kind = ClaimKind.RECOMMENDATION

    issues = cross_module_review([first_draft, second_draft], [first, second])

    conflicts = [issue for issue in issues if issue.kind == "action_conflict"]
    assert len(conflicts) == 2
    assert all(issue.severity == "blocking" for issue in conflicts)

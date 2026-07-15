"""Structural and deterministic semantic checks for module claims."""

from ..models import EvidenceItem, ModuleDraft, ReviewIssue
from ..skills.resolver import SkillResolver

_FALSE_OVERLOAD_PHRASES = (
    "已过载",
    "过载运行",
    "当前过载",
    "负荷率超过100",
)


def audit_draft(
    draft: ModuleDraft,
    evidence_items: list[EvidenceItem],
    skills: SkillResolver,
) -> list[ReviewIssue]:
    """Audit claim traceability before any prose is eligible for rendering."""

    evidence_by_id = {item.id: item for item in evidence_items}
    issues: list[ReviewIssue] = []
    for claim in draft.claims:
        expected_skill = skills.resolve(claim.submodule_id).reference
        if expected_skill not in claim.skill_ids:
            issues.append(
                ReviewIssue(
                    module_id=draft.module_id,
                    submodule_id=claim.submodule_id,
                    claim_id=claim.id,
                    kind="unapproved_skill",
                    message=f"Claim 未引用批准的 Skill {expected_skill}",
                    severity="blocking",
                    round=draft.revision,
                )
            )
        if not claim.evidence_ids and claim.unverified:
            continue
        for evidence_id in claim.evidence_ids:
            item = evidence_by_id.get(evidence_id)
            if item is None:
                issues.append(
                    ReviewIssue(
                        module_id=draft.module_id,
                        submodule_id=claim.submodule_id,
                        claim_id=claim.id,
                        kind="unknown_evidence",
                        message=f"未知证据引用：{evidence_id}",
                        severity="blocking",
                        round=draft.revision,
                    )
                )
                continue
            if item.module_id != claim.module_id or item.submodule_id != claim.submodule_id:
                issues.append(
                    ReviewIssue(
                        module_id=draft.module_id,
                        submodule_id=claim.submodule_id,
                        claim_id=claim.id,
                        kind="wrong_submodule",
                        message=(
                            f"证据 {evidence_id} 属于 {item.submodule_id}，"
                            f"不能支持 {claim.submodule_id}"
                        ),
                        severity="blocking",
                        round=draft.revision,
                    )
                )
                continue
            if (
                item.submodule_id == "2.4.1.1"
                and item.unit == "%"
                and isinstance(item.value, (int, float))
                and float(item.value) < 100
                and any(phrase in claim.text for phrase in _FALSE_OVERLOAD_PHRASES)
            ):
                issues.append(
                    ReviewIssue(
                        module_id=draft.module_id,
                        submodule_id=claim.submodule_id,
                        claim_id=claim.id,
                        kind="threshold_misuse",
                        message=(
                            f"证据 {evidence_id} 的负荷率低于100%，Claim 却将其表述为当前过载"
                        ),
                        severity="blocking",
                        round=draft.revision,
                    )
                )
            if item.needs_confirmation and "=NG" in item.fact.upper() and not item.photo_refs:
                issues.append(
                    ReviewIssue(
                        module_id=draft.module_id,
                        submodule_id=claim.submodule_id,
                        claim_id=None,
                        kind="missing_photo",
                        message=f"证据 {evidence_id} 为 NG，但缺少同一行照片",
                        severity="warning",
                        round=draft.revision,
                    )
                )
    unique: dict[tuple, ReviewIssue] = {}
    for issue in issues:
        key = (
            issue.module_id,
            issue.submodule_id,
            issue.claim_id,
            issue.kind,
            issue.message,
        )
        unique[key] = issue
    return list(unique.values())

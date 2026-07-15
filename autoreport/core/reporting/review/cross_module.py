"""Deterministic consistency checks across independently drafted modules."""

from collections import defaultdict
from itertools import combinations

from ..models import ClaimKind, EvidenceItem, ModuleDraft, ReviewIssue


def _normalized(text: str) -> str:
    return "".join(text.split()).rstrip("。；;.")


def cross_module_review(
    drafts: list[ModuleDraft],
    evidence_items: list[EvidenceItem],
) -> list[ReviewIssue]:
    issues: list[ReviewIssue] = []
    metric_groups: dict[tuple[str, str, str, str, str], list[EvidenceItem]] = defaultdict(list)
    for item in evidence_items:
        if item.module_id and item.submodule_id and isinstance(item.value, (int, float)):
            metric_groups[
                (
                    _normalized(item.subject),
                    item.unit or "",
                    str(item.source.path),
                    item.source.sheet or "",
                    item.source.cell or "",
                )
            ].append(item)
    for (subject, unit, _path, _sheet, _cell), items in metric_groups.items():
        modules = {item.module_id for item in items}
        values = {round(float(item.value), 6) for item in items}
        if len(modules) < 2 or len(values) < 2:
            continue
        detail = ", ".join(f"{item.id}={item.value}{unit}" for item in items)
        for item in items:
            issues.append(
                ReviewIssue(
                    module_id=item.module_id or "",
                    submodule_id=item.submodule_id,
                    kind="metric_conflict",
                    message=f"跨模块指标 {subject} 数值不一致：{detail}",
                    severity="blocking",
                )
            )

    evidence_by_id = {item.id: item for item in evidence_items}
    seen_claims: dict[str, tuple[str, str, frozenset[tuple[str, str, str]]]] = {}
    for draft in sorted(drafts, key=lambda item: item.module_id):
        for claim in draft.claims:
            if claim.kind is not ClaimKind.FACT or not claim.evidence_ids:
                continue
            key = _normalized(claim.text)
            sources = frozenset(
                (
                    str(evidence_by_id[evidence_id].source.path),
                    evidence_by_id[evidence_id].source.sheet or "",
                    evidence_by_id[evidence_id].source.cell or "",
                )
                for evidence_id in claim.evidence_ids
                if evidence_id in evidence_by_id
            )
            previous = seen_claims.get(key)
            if previous and previous[0] != claim.module_id and previous[2] != sources:
                issues.append(
                    ReviewIssue(
                        module_id=claim.module_id,
                        submodule_id=claim.submodule_id,
                        claim_id=claim.id,
                        kind="duplicate_claim",
                        message=f"与 {previous[0]} 的 Claim {previous[1]} 内容重复",
                        severity="warning",
                    )
                )
            else:
                seen_claims[key] = (claim.module_id, claim.id, sources)

    actions_by_subject: dict[str, list] = defaultdict(list)
    for draft in drafts:
        for claim in draft.claims:
            if claim.kind is not ClaimKind.RECOMMENDATION:
                continue
            subjects = {
                _normalized(evidence_by_id[evidence_id].subject)
                for evidence_id in claim.evidence_ids
                if evidence_id in evidence_by_id
            }
            for subject in subjects:
                actions_by_subject[subject].append(claim)
    opposites = (("停运", "继续运行"), ("更换", "继续使用"), ("投入", "退出"))
    for subject, claims in actions_by_subject.items():
        for first, second in combinations(claims, 2):
            if first.module_id == second.module_id:
                continue
            if not any(
                (left in first.text and right in second.text)
                or (right in first.text and left in second.text)
                for left, right in opposites
            ):
                continue
            message = f"同一对象 {subject} 存在相反行动：{first.text} / {second.text}"
            for claim in (first, second):
                issues.append(
                    ReviewIssue(
                        module_id=claim.module_id,
                        submodule_id=claim.submodule_id,
                        claim_id=claim.id,
                        kind="action_conflict",
                        message=message,
                        severity="blocking",
                    )
                )

    unique: dict[tuple[str, str | None, str | None, str, str], ReviewIssue] = {}
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

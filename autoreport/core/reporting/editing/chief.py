"""Assemble executive summaries while treating module claims as immutable."""

from ..models import Claim, ClaimKind, ModuleDraft, ReportingModel, ReviewIssue


class ProtectedClaimError(ValueError):
    pass


class RiskSummaryItem(ReportingModel):
    module_id: str
    submodule_id: str
    text: str
    claim_ids: list[str]
    evidence_ids: list[str]
    priority: str


class PriorityAction(ReportingModel):
    module_id: str
    submodule_id: str
    text: str
    claim_ids: list[str]
    evidence_ids: list[str]
    priority: str


class ChiefEditorResult(ReportingModel):
    overview: str
    risk_summary: list[RiskSummaryItem]
    actions: list[PriorityAction]
    protected_claim_ids: list[str]
    cross_module_issues: list[ReviewIssue]


def _priority(claim: Claim) -> str:
    text = claim.text
    if any(token in text for token in ("触电", "火灾", "停运", "立即", "超过100%")):
        return "高"
    if any(token in text for token in ("异常", "缺少", "复核", "整改")):
        return "中"
    return "低"


def _protected(claim: Claim) -> tuple:
    return (
        claim.module_id,
        claim.submodule_id,
        claim.kind,
        claim.text,
        tuple(claim.evidence_ids),
        tuple(claim.skill_ids),
        claim.unverified,
    )


class ChiefEditor:
    def compile(
        self,
        drafts: list[ModuleDraft],
        cross_module_issues: list[ReviewIssue],
    ) -> ChiefEditorResult:
        claims = [
            claim
            for draft in sorted(drafts, key=lambda item: item.module_id)
            for claim in draft.claims
        ]
        source_for_summary = [claim for claim in claims if claim.kind is ClaimKind.RISK]
        if not source_for_summary:
            source_for_summary = [
                claim for claim in claims if claim.kind in {ClaimKind.FACT, ClaimKind.CONCLUSION}
            ]
        risk_by_key: dict[tuple[str, str, str], RiskSummaryItem] = {}
        for claim in source_for_summary:
            key = (claim.module_id, claim.submodule_id, "".join(claim.text.split()))
            item = risk_by_key.get(key)
            if item is None:
                risk_by_key[key] = RiskSummaryItem(
                    module_id=claim.module_id,
                    submodule_id=claim.submodule_id,
                    text=claim.text,
                    claim_ids=[claim.id],
                    evidence_ids=list(claim.evidence_ids),
                    priority=_priority(claim),
                )
                continue
            item.claim_ids.append(claim.id)
            item.evidence_ids = list(dict.fromkeys([*item.evidence_ids, *claim.evidence_ids]))
        risk_summary = list(risk_by_key.values())

        actions_by_text: dict[str, PriorityAction] = {}
        for claim in claims:
            if claim.kind is not ClaimKind.RECOMMENDATION:
                continue
            normalized = "".join(claim.text.split())
            action = actions_by_text.get(normalized)
            if action is not None:
                action.claim_ids.append(claim.id)
                action.evidence_ids = list(
                    dict.fromkeys([*action.evidence_ids, *claim.evidence_ids])
                )
                continue
            actions_by_text[normalized] = PriorityAction(
                module_id=claim.module_id,
                submodule_id=claim.submodule_id,
                text=claim.text,
                claim_ids=[claim.id],
                evidence_ids=list(claim.evidence_ids),
                priority=_priority(claim),
            )
        actions = list(actions_by_text.values())
        priority_order = {"高": 0, "中": 1, "低": 2}
        actions.sort(key=lambda action: (priority_order[action.priority], action.module_id))
        module_ids = sorted({draft.module_id for draft in drafts})
        overview = (
            f"本报告覆盖 {len(module_ids)} 个评估模块；"
            f"形成 {len(risk_summary)} 条风险/状态摘要和 {len(actions)} 条改善行动。"
        )
        return ChiefEditorResult(
            overview=overview,
            risk_summary=risk_summary,
            actions=actions,
            protected_claim_ids=[claim.id for claim in claims],
            cross_module_issues=cross_module_issues,
        )

    @staticmethod
    def validate_protected_claims(
        original_drafts: list[ModuleDraft],
        edited_drafts: list[ModuleDraft],
    ) -> None:
        original = {
            claim.id: _protected(claim) for draft in original_drafts for claim in draft.claims
        }
        edited = {claim.id: _protected(claim) for draft in edited_drafts for claim in draft.claims}
        for claim_id, protected in original.items():
            if edited.get(claim_id) != protected:
                raise ProtectedClaimError(f"protected claim changed or removed: {claim_id}")

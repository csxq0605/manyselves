"""Evidence-bound deterministic worker for report modules other than 2.4."""

from collections import defaultdict

from ..models import Claim, ClaimKind, EvidenceItem, ModuleDraft, ModuleTask
from ..skills.resolver import SkillResolver
from ..taxonomy import REPORT_TAXONOMY
from .base import make_claim

_KIND_LABELS = {
    ClaimKind.FACT: "事实",
    ClaimKind.CONCLUSION: "判断",
    ClaimKind.RISK: "风险",
    ClaimKind.RECOMMENDATION: "建议",
}


def render_module_markdown(module_id: str, claims: list[Claim]) -> str:
    module = REPORT_TAXONOMY[module_id]
    claims_by_submodule: dict[str, list[Claim]] = defaultdict(list)
    for claim in claims:
        claims_by_submodule[claim.submodule_id].append(claim)
    lines = [f"# {module_id} {module.title}", ""]
    for submodule_id, definition in module.submodules.items():
        submodule_claims = claims_by_submodule.get(submodule_id)
        if not submodule_claims:
            continue
        lines.extend([f"## {submodule_id} {definition.title}", ""])
        for claim in submodule_claims:
            evidence = ", ".join(claim.evidence_ids) or "未核实"
            skills = ", ".join(claim.skill_ids)
            lines.append(
                f"- **{_KIND_LABELS[claim.kind]}**：{claim.text} "
                f"`[evidence: {evidence}; skill: {skills}]`"
            )
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


class GenericModuleWorker:
    def __init__(self, module_id: str, skills: SkillResolver):
        if module_id == "2.4" or module_id not in REPORT_TAXONOMY:
            raise ValueError(f"GenericModuleWorker cannot handle {module_id}")
        self.module_id = module_id
        self.skills = skills

    def run(self, task: ModuleTask, evidence_items: list[EvidenceItem]) -> ModuleDraft:
        if task.module_id != self.module_id:
            raise ValueError(f"worker {self.module_id} cannot handle {task.module_id}")
        evidence_by_id = {item.id: item for item in evidence_items}
        claims_by_submodule: dict[str, list[Claim]] = defaultdict(list)

        for submodule_id, evidence_ids in task.submodule_evidence.items():
            skill = self.skills.resolve(submodule_id)
            for evidence_id in evidence_ids:
                try:
                    item = evidence_by_id[evidence_id]
                except KeyError as exc:
                    raise ValueError(f"task references unknown evidence {evidence_id}") from exc
                if item.module_id != self.module_id or item.submodule_id != submodule_id:
                    raise ValueError(
                        f"evidence {evidence_id} is outside assigned submodule {submodule_id}"
                    )
                claims_by_submodule[submodule_id].extend(
                    self._claims_for_item(item, skill.reference)
                )

        for submodule_id in task.missing_submodules:
            if not task.allow_unverified:
                raise ValueError(f"missing submodule {submodule_id} requires allow_unverified=true")
            skill = self.skills.resolve(submodule_id)
            claims_by_submodule[submodule_id].append(
                make_claim(
                    module_id=self.module_id,
                    submodule_id=submodule_id,
                    kind=ClaimKind.CONCLUSION,
                    text="缺少可追溯客户证据，本子模块无法形成核实结论。",
                    evidence_ids=[],
                    skill_ids=[skill.reference],
                    unverified=True,
                )
            )

        claims = [
            claim
            for submodule_id in REPORT_TAXONOMY[self.module_id].submodules
            for claim in claims_by_submodule.get(submodule_id, [])
        ]
        return ModuleDraft(
            module_id=self.module_id,
            markdown=render_module_markdown(self.module_id, claims),
            evidence_ids=task.evidence_ids,
            claims=claims,
            unverified_items=task.missing_submodules,
            revision=task.revision,
        )

    def _claims_for_item(self, item: EvidenceItem, skill_id: str) -> list[Claim]:
        common = {
            "module_id": self.module_id,
            "submodule_id": item.submodule_id,
            "evidence_ids": [item.id],
            "skill_ids": [skill_id],
        }
        claims = [make_claim(**common, kind=ClaimKind.FACT, text=f"{item.subject}：{item.fact}")]
        if item.needs_confirmation:
            claims.extend(
                [
                    make_claim(
                        **common,
                        kind=ClaimKind.CONCLUSION,
                        text="该条为既有评估或缺少配套材料的记录，需回到原始证据复核。",
                    ),
                    make_claim(
                        **common,
                        kind=ClaimKind.RECOMMENDATION,
                        text="补充原始记录、测点或同一位置照片后再形成最终判断。",
                    ),
                ]
            )
            return claims

        if item.submodule_id == "2.1.1" and item.unit == "%" and item.value is not None:
            load_rate = float(item.value)
            conclusion = (
                f"本次计算负荷率为{load_rate:.2f}%，未达到100%，"
                "不能据此认定设备当前处于超过额定负荷的运行状态。"
                if load_rate < 100
                else f"本次计算负荷率为{load_rate:.2f}%，达到或超过100%。"
            )
            claims.append(make_claim(**common, kind=ClaimKind.CONCLUSION, text=conclusion))
            if load_rate >= 80:
                claims.append(
                    make_claim(
                        **common,
                        kind=ClaimKind.RISK,
                        text="容量余量较小，负荷继续增长或运行波动时存在进入过载区间的风险。",
                    )
                )
            return claims

        abnormal = any(token in item.fact.upper() for token in ("=NG", "异常", "缺少"))
        if abnormal:
            claims.extend(
                [
                    make_claim(
                        **common,
                        kind=ClaimKind.CONCLUSION,
                        text="该检查项在客户资料中记录为异常或缺失。",
                    ),
                    make_claim(
                        **common,
                        kind=ClaimKind.RISK,
                        text="该异常可能降低相应系统的安全运行、故障防护或运维保障能力。",
                    ),
                    make_claim(
                        **common,
                        kind=ClaimKind.RECOMMENDATION,
                        text="复核原始资料与现场状态，完成整改并留存可追溯复验记录。",
                    ),
                ]
            )
        else:
            claims.append(
                make_claim(
                    **common,
                    kind=ClaimKind.CONCLUSION,
                    text="按本次客户记录，该检查项未标记异常。",
                )
            )
        return claims

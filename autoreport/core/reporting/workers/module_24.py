"""Evidence-bound deterministic worker for report module 2.4."""

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

_RISK_TEXT = {
    "2.4.1.3": "安全连锁或闭锁异常会增加误操作不能被强制阻止的风险。",
    "2.4.2.1": "裸露导体防护异常会增加直接接触或绝缘故障风险。",
    "2.4.2.2": "等电位连接或接地异常会削弱故障电流通路和间接接触防护。",
    "2.4.2.3": "电气连接异常可能造成接触电阻增大与局部发热。",
    "2.4.2.4": "标牌或色标异常会增加识别和操作差错风险。",
    "2.4.2.5": "电缆、桥架或母线安装异常可能造成机械损伤、连接受力或防护失效。",
    "2.4.2.6": "柜体防护或封堵异常会增加异物、潮气或火烟蔓延风险。",
    "2.4.4": "现场检查异常可能影响设备维护条件或人员安全。",
}


def render_module_24_markdown(claims: list[Claim]) -> str:
    claims_by_submodule: dict[str, list[Claim]] = defaultdict(list)
    for claim in claims:
        claims_by_submodule[claim.submodule_id].append(claim)
    lines = [f"# 2.4 {REPORT_TAXONOMY['2.4'].title}", ""]
    definitions = REPORT_TAXONOMY["2.4"].submodules
    for submodule_id, definition in definitions.items():
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


class Module24Worker:
    def __init__(self, skills: SkillResolver):
        self.skills = skills

    def run(
        self,
        task: ModuleTask,
        evidence_items: list[EvidenceItem],
    ) -> ModuleDraft:
        if task.module_id != "2.4":
            raise ValueError(f"Module24Worker cannot handle {task.module_id}")
        evidence_by_id = {item.id: item for item in evidence_items}
        claims_by_submodule: dict[str, list[Claim]] = defaultdict(list)

        for submodule_id, evidence_ids in task.submodule_evidence.items():
            skill = self.skills.resolve(submodule_id)
            for evidence_id in evidence_ids:
                try:
                    item = evidence_by_id[evidence_id]
                except KeyError as exc:
                    raise ValueError(f"task references unknown evidence {evidence_id}") from exc
                if item.module_id != "2.4" or item.submodule_id != submodule_id:
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
                    module_id="2.4",
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
            for submodule_id in REPORT_TAXONOMY["2.4"].submodules
            for claim in claims_by_submodule.get(submodule_id, [])
        ]
        markdown = render_module_24_markdown(claims)
        return ModuleDraft(
            module_id="2.4",
            markdown=markdown,
            evidence_ids=task.evidence_ids,
            claims=claims,
            unverified_items=task.missing_submodules,
            revision=task.revision,
        )

    def _claims_for_item(self, item: EvidenceItem, skill_id: str) -> list[Claim]:
        common = {
            "module_id": "2.4",
            "submodule_id": item.submodule_id,
            "evidence_ids": [item.id],
            "skill_ids": [skill_id],
        }
        claims = [
            make_claim(
                **common,
                kind=ClaimKind.FACT,
                text=f"{item.subject}：{item.fact}",
            )
        ]
        if item.needs_confirmation:
            claims.extend(
                [
                    make_claim(
                        **common,
                        kind=ClaimKind.CONCLUSION,
                        text="该条为既有评估或缺少配套材料的记录，需回到原始测量、照片或现场记录复核。",
                    ),
                    make_claim(
                        **common,
                        kind=ClaimKind.RECOMMENDATION,
                        text="补充原始测量值、同一行照片及现场位置后再形成最终判断。",
                    ),
                ]
            )
            return claims

        if item.submodule_id == "2.4.1.1" and item.unit == "%" and item.value is not None:
            load_rate = float(item.value)
            if load_rate < 100:
                conclusion = (
                    f"本次计算负荷率为{load_rate:.2f}%，未达到100%，"
                    "不能据此认定设备当前处于超过额定负荷的运行状态。"
                )
            else:
                conclusion = f"本次计算负荷率为{load_rate:.2f}%，达到或超过100%。"
            claims.append(make_claim(**common, kind=ClaimKind.CONCLUSION, text=conclusion))
            if load_rate >= 80:
                claims.extend(
                    [
                        make_claim(
                            **common,
                            kind=ClaimKind.RISK,
                            text="当前容量余量较小，负荷继续增长或运行波动时存在达到过载区间的风险。",
                        ),
                        make_claim(
                            **common,
                            kind=ClaimKind.RECOMMENDATION,
                            text="持续监测负荷率并复核峰值工况，在新增负荷前完成容量校核与负荷平衡。",
                        ),
                    ]
                )
            return claims

        if item.submodule_id == "2.4.3.1" and item.unit == "A" and item.value is not None:
            residual = float(item.value)
            conclusion = (
                f"实测剩余电流为{residual:g}A；该值触发工作表的10A附图/复核条件，"
                "该触发值不是法定安全限值。"
            )
            claims.extend(
                [
                    make_claim(**common, kind=ClaimKind.CONCLUSION, text=conclusion),
                    make_claim(
                        **common,
                        kind=ClaimKind.RISK,
                        text="异常剩余电流可能与零地混接、绝缘下降或漏电有关；仅凭该数值不能确定原因或直接认定火灾状态。",
                    ),
                    make_claim(
                        **common,
                        kind=ClaimKind.RECOMMENDATION,
                        text="分回路排查零地连接和绝缘状态，复测并保存测点、工况及同一行照片。",
                    ),
                ]
            )
            return claims

        fact_upper = item.fact.upper()
        if "=NG" in fact_upper or "异常" in item.fact or "缺少" in item.fact:
            claims.extend(
                [
                    make_claim(
                        **common,
                        kind=ClaimKind.CONCLUSION,
                        text="该检查项在客户诊断表中记录为异常。",
                    ),
                    make_claim(
                        **common,
                        kind=ClaimKind.RISK,
                        text=_RISK_TEXT.get(
                            item.submodule_id,
                            "该异常可能降低设备安全运行或维护条件。",
                        ),
                    ),
                    make_claim(
                        **common,
                        kind=ClaimKind.RECOMMENDATION,
                        text="复核现场位置与照片，按对应检查要求整改并留存复验记录。",
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

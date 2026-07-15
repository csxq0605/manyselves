from pathlib import Path

from autoreport.core.reporting.models import (
    ClaimKind,
    EvidenceItem,
    ModuleTask,
    SourceLocation,
)
from autoreport.core.reporting.skills.resolver import SkillResolver
from autoreport.core.reporting.taxonomy import REPORT_TAXONOMY
from autoreport.core.reporting.workers.module_24 import Module24Worker


def _evidence(
    *,
    evidence_id: str,
    submodule_id: str,
    subject: str,
    fact: str,
    value: float | None = None,
    unit: str | None = None,
    photo_refs: list[str] | None = None,
) -> EvidenceItem:
    return EvidenceItem(
        id=evidence_id,
        subject=subject,
        fact=fact,
        source=SourceLocation(
            file_id="file-s44",
            path=Path("Inputs/S4-4诊断工作用表.xlsx"),
            sheet="低配评估详情",
            cell="J5:K5",
            row=5,
            column="J",
        ),
        module_id="2.4",
        submodule_id=submodule_id,
        value=value,
        unit=unit,
        photo_refs=photo_refs or [],
    )


def test_packaged_skills_cover_every_report_submodule() -> None:
    resolver = SkillResolver.packaged()

    assert all(
        resolver.resolve(submodule_id).reference.endswith("@1.0.0")
        for module in REPORT_TAXONOMY.values()
        for submodule_id in module.submodules
    )


def test_module_24_worker_emits_evidence_bound_claim_chain() -> None:
    evidence = _evidence(
        evidence_id="ev-residual",
        submodule_id="2.4.3.1",
        subject="车间配电房/1A2",
        fact="实测剩余电流=46.8A",
        value=46.8,
        unit="A",
        photo_refs=["ID_RESIDUAL"],
    )
    task = ModuleTask(
        id="module-2.4",
        module_id="2.4",
        evidence_ids=[evidence.id],
        submodule_evidence={"2.4.3.1": [evidence.id]},
    )

    draft = Module24Worker(SkillResolver.packaged()).run(task, [evidence])

    kinds = {claim.kind for claim in draft.claims}
    assert {
        ClaimKind.FACT,
        ClaimKind.CONCLUSION,
        ClaimKind.RISK,
        ClaimKind.RECOMMENDATION,
    } <= kinds
    assert all(claim.evidence_ids == ["ev-residual"] for claim in draft.claims)
    assert all(claim.skill_ids for claim in draft.claims)
    assert all("@" in skill_id for claim in draft.claims for skill_id in claim.skill_ids)
    assert "# 2.4 配电设备/元件风险" in draft.markdown
    assert "2.4.3.1 低压回路剩余电流过大" in draft.markdown


def test_module_24_worker_does_not_turn_96_99_percent_into_existing_overload() -> None:
    evidence = _evidence(
        evidence_id="ev-load",
        submodule_id="2.4.1.1",
        subject="车间配电房/2A2",
        fact="运行电流=3500A；变压器容量=2500kVA；计算负荷率=96.99%",
        value=96.992,
        unit="%",
    )
    task = ModuleTask(
        id="module-2.4",
        module_id="2.4",
        evidence_ids=[evidence.id],
        submodule_evidence={"2.4.1.1": [evidence.id]},
    )

    draft = Module24Worker(SkillResolver.packaged()).run(task, [evidence])
    text = "\n".join(claim.text for claim in draft.claims)

    assert "已过载" not in text
    assert "过载运行" not in text
    assert "未达到100%" in text
    assert "负荷继续增长" in text


def test_module_24_worker_records_permitted_missing_evidence_as_unverified() -> None:
    task = ModuleTask(
        id="module-2.4",
        module_id="2.4",
        missing_submodules=["2.4.2.2"],
        allow_unverified=True,
    )

    draft = Module24Worker(SkillResolver.packaged()).run(task, [])

    assert draft.unverified_items == ["2.4.2.2"]
    assert len(draft.claims) == 1
    assert draft.claims[0].unverified is True
    assert draft.claims[0].evidence_ids == []

from pathlib import Path

import pytest

from autoreport.core.reporting.models import EvidenceItem, ModuleTask, SourceLocation
from autoreport.core.reporting.skills.resolver import SkillResolver
from autoreport.core.reporting.workers.generic import GenericModuleWorker


def _evidence(
    module_id: str,
    submodule_id: str,
    fact: str,
    *,
    value: float | None = None,
    unit: str | None = None,
) -> EvidenceItem:
    return EvidenceItem(
        id=f"ev-{submodule_id}",
        subject="1#变压器",
        fact=fact,
        source=SourceLocation(
            file_id="file-s44",
            path=Path("Inputs/S4-4诊断工作用表.xlsx"),
            sheet="低配评估详情",
            cell="C4:E4",
        ),
        module_id=module_id,
        submodule_id=submodule_id,
        value=value,
        unit=unit,
    )


def test_generic_worker_emits_traceable_claims_for_non_24_module() -> None:
    evidence = _evidence("2.2", "2.2.1.1", "谐波=>6.2%")
    task = ModuleTask(
        id="module-2.2",
        module_id="2.2",
        evidence_ids=[evidence.id],
        submodule_evidence={"2.2.1.1": [evidence.id]},
    )

    draft = GenericModuleWorker("2.2", SkillResolver.packaged()).run(task, [evidence])

    assert draft.module_id == "2.2"
    assert "# 2.2 环境工况风险" in draft.markdown
    assert "2.2.1.1 谐波风险情况" in draft.markdown
    assert all(claim.evidence_ids == [evidence.id] for claim in draft.claims)
    assert all(claim.skill_ids == ["pds.module22.environment@1.0.0"] for claim in draft.claims)


def test_generic_worker_preserves_below_100_load_semantics() -> None:
    evidence = _evidence(
        "2.1",
        "2.1.1",
        "计算负荷率=96.99%",
        value=96.992,
        unit="%",
    )
    task = ModuleTask(
        id="module-2.1",
        module_id="2.1",
        evidence_ids=[evidence.id],
        submodule_evidence={"2.1.1": [evidence.id]},
    )

    draft = GenericModuleWorker("2.1", SkillResolver.packaged()).run(task, [evidence])
    text = "\n".join(claim.text for claim in draft.claims)

    assert "未达到100%" in text
    assert "已过载" not in text


def test_generic_worker_rejects_cross_module_evidence() -> None:
    evidence = _evidence("2.3", "2.3.3", "电涌保护装置=NG")
    task = ModuleTask(
        id="module-2.2",
        module_id="2.2",
        evidence_ids=[evidence.id],
        submodule_evidence={"2.2.1.1": [evidence.id]},
    )

    with pytest.raises(ValueError, match="outside assigned submodule"):
        GenericModuleWorker("2.2", SkillResolver.packaged()).run(task, [evidence])

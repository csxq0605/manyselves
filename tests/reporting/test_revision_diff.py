from copy import deepcopy

from autoreport.core.reporting.agentic_models import ClaimRecord, ModuleSubmission
from autoreport.core.reporting.revision_diff import build_revision_diff
from autoreport.core.reporting.taxonomy import REPORT_TAXONOMY


def _submission() -> ModuleSubmission:
    module_id = "2.4"
    return ModuleSubmission(
        module_id=module_id,
        markdown="设备模块原始正文",
        submodule_narratives={
            submodule_id: f"{submodule_id} 原始正文"
            for submodule_id in REPORT_TAXONOMY[module_id].submodules
        },
        claims=[
            ClaimRecord(
                id="C-2.4.1.1-001",
                module_id=module_id,
                submodule_id="2.4.1.1",
                text="容量判断",
                claim_type="technical_interpretation",
                source_ids=["R-capacity"],
            ),
            ClaimRecord(
                id="C-2.4.2.2-001",
                module_id=module_id,
                submodule_id="2.4.2.2",
                text="接地判断",
                claim_type="technical_interpretation",
                source_ids=["R-ground"],
            ),
        ],
        source_ids=["R-capacity", "R-ground"],
        unresolved_questions=[],
        revision=0,
    )


def test_revision_diff_reports_all_changes_without_rejecting_them() -> None:
    before = _submission()
    payload = deepcopy(before.model_dump(mode="python"))
    payload["revision"] = 1
    payload["submodule_narratives"]["2.4.2.2"] = "为保持一致性同步调整"
    payload["claims"][1]["source_ids"] = ["R-other"]
    payload["source_ids"] = ["R-capacity", "R-other"]
    revised = ModuleSubmission.model_validate(payload)

    diff = build_revision_diff(before, revised)

    assert diff["changed_submodule_narratives"] == ["2.4.2.2"]
    assert diff["changed_claim_ids"] == ["C-2.4.2.2-001"]
    assert diff["source_ids_added"] == ["R-other"]
    assert diff["source_ids_removed"] == ["R-ground"]

from copy import deepcopy

import pytest

from autoreport.core.reporting.agentic_models import ClaimRecord, ModuleSubmission
from autoreport.core.reporting.revision_guard import RevisionGuard
from autoreport.core.reporting.taxonomy import REPORT_TAXONOMY


def _submission() -> ModuleSubmission:
    module_id = "2.4"
    narratives = {
        submodule_id: f"{submodule_id} 原始正文"
        for submodule_id in REPORT_TAXONOMY[module_id].submodules
    }
    return ModuleSubmission(
        module_id=module_id,
        markdown="设备模块原始正文",
        submodule_narratives=narratives,
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


def test_revision_guard_accepts_changes_only_inside_target_submodule() -> None:
    before = _submission()
    payload = before.model_dump(mode="python")
    payload["revision"] = 1
    payload["submodule_narratives"]["2.4.1.1"] = "2.4.1.1 修订正文"
    payload["claims"][0]["text"] = "修订后的容量判断"
    revised = ModuleSubmission.model_validate(payload)

    RevisionGuard.validate(before, revised, {"2.4.1.1"})


def test_revision_guard_rejects_unrelated_submodule_drift() -> None:
    before = _submission()
    payload = deepcopy(before.model_dump(mode="python"))
    payload["revision"] = 1
    payload["submodule_narratives"]["2.4.2.2"] = "不应变化"
    revised = ModuleSubmission.model_validate(payload)

    with pytest.raises(ValueError, match="2.4.2.2"):
        RevisionGuard.validate(before, revised, {"2.4.1.1"})


def test_revision_guard_rejects_unrelated_claim_or_source_drift() -> None:
    before = _submission()
    payload = deepcopy(before.model_dump(mode="python"))
    payload["revision"] = 1
    payload["claims"][1]["source_ids"] = ["R-other"]
    revised = ModuleSubmission.model_validate(payload)

    with pytest.raises(ValueError, match="2.4.2.2"):
        RevisionGuard.validate(before, revised, {"2.4.1.1"})

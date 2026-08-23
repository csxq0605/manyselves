from __future__ import annotations

from copy import deepcopy

import pytest
from pydantic import ValidationError

from manyselves.capabilities.distribution_reporting.domain.taxonomy import REPORT_TAXONOMY
from manyselves.core.reporting.agent_runner import ReportingAgentRunner
from manyselves.core.reporting.cross_specialization import (
    CROSS_LANE_SPECIALIZATIONS,
)
from manyselves.core.reporting.input_contracts import (
    INPUT_CONTRACT_EXAMPLES,
    CrossReviewInput,
)


def test_every_cross_lane_has_a_distinct_contract_review_focus() -> None:
    assert set(CROSS_LANE_SPECIALIZATIONS) == set(REPORT_TAXONOMY)
    focuses = {
        specialization.review_focus
        for specialization in CROSS_LANE_SPECIALIZATIONS.values()
    }
    assert len(focuses) == 5
    for module_id, specialization in CROSS_LANE_SPECIALIZATIONS.items():
        context = specialization.prompt_context()
        assert f'owner_module_id="{module_id}"' in context
        assert "不是模块写作 Skill" in context


def test_cross_recheck_rejects_a_finding_owned_by_another_lane() -> None:
    payload = deepcopy(INPUT_CONTRACT_EXAMPLES["cross_review_input"])
    payload.update(
        {
            "phase": "recheck",
            "owner_module_id": "2.1",
            "modules": {},
            "changed_module_ids": [],
            "required_findings": [
                {
                    "id": "XMR-2.3-001",
                    "owner_module_id": "2.3",
                    "target_submodule_ids": ["2.3.1"],
                    "related_module_ids": ["2.4"],
                    "category": "dependencies",
                    "impact": "blocking",
                    "observation": (
                        "保护模块当前正文遗漏了设备模块提供的跨模块实施前提、"
                        "风险影响、实施顺序和联合验收接口，无法形成完整闭环。"
                    ),
                    "evidence_refs": [payload["module_refs"]["2.3"]],
                    "required_change": "在保护模块目标小节补充设备能力前提、实施顺序和联合验收方法。",
                    "reviewer_checks": ["确认跨模块前提、顺序和联合验收均已写回"],
                }
            ],
        }
    )
    with pytest.raises(ValidationError, match="owner_module_id"):
        CrossReviewInput.model_validate(payload)


def test_cross_provider_schema_locks_finding_owner_and_targets_to_lane() -> None:
    contract = CrossReviewInput.model_validate(
        INPUT_CONTRACT_EXAMPLES["cross_review_input"]
    )
    schema = ReportingAgentRunner._task_submission_schema(
        "cross_review_finding_submission",
        contract,
    )
    finding = schema["$defs"]["CrossReviewFinding"]
    assert finding["properties"]["owner_module_id"]["const"] == "2.1"
    assert set(
        finding["properties"]["target_submodule_ids"]["items"]["enum"]
    ) == set(REPORT_TAXONOMY["2.1"].submodules)

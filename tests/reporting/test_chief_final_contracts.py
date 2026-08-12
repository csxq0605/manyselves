from __future__ import annotations

import pytest
from pydantic import ValidationError

from manyselves.core.reporting.agentic_models import (
    CrossDecisionIFClosure,
    CrossDecisionPack,
    CrossDecisionXMRVerdict,
    FinalReviewFinding,
)
from manyselves.core.reporting.input_contracts import (
    CrossDecisionPackView,
    INPUT_CONTRACT_EXAMPLES,
    INPUT_CONTRACT_TYPES,
)


RUN = "run-contract"
COMPLETION = f"Work/runs/{RUN}/reviews/cross-completion.json"


def _pack_payload() -> dict:
    return {
        "run_id": RUN,
        "module_ids": ["2.1", "2.2", "2.3", "2.4", "2.5"],
        "cross_review_completion_ref": COMPLETION,
        "synthesis_inputs": [],
        "if_closures": [],
        "xmr_verdicts": [],
        "residual_risks": [],
        "artifact_sha256": {COMPLETION: "0" * 64},
        "pack_sha256": "0" * 64,
    }


def test_cross_decision_pack_requires_terminal_five_module_hash_bound_state() -> None:
    pack = CrossDecisionPack.model_validate(_pack_payload())
    assert set(pack.module_ids) == {"2.1", "2.2", "2.3", "2.4", "2.5"}

    with pytest.raises(ValidationError):
        CrossDecisionPack.model_validate(
            {**_pack_payload(), "module_ids": ["2.1", "2.2"]}
        )
    with pytest.raises(ValidationError, match="artifact_sha256"):
        CrossDecisionPack.model_validate(
            {**_pack_payload(), "artifact_sha256": {}}
        )
    with pytest.raises(ValidationError, match="current run"):
        CrossDecisionPack.model_validate(
            {
                **_pack_payload(),
                "cross_review_completion_ref": "Work/runs/foreign/reviews/cross.json",
                "artifact_sha256": {
                    "Work/runs/foreign/reviews/cross.json": "0" * 64
                },
            }
        )


def test_if_closure_rejects_pending_and_missing_evidence() -> None:
    base = {
        "request_id": "IF-2.1-2.3-001",
        "requester_submodule_id": "2.1.1",
        "target_submodule_id": "2.3.1",
        "question": "当前联锁是否覆盖异常切换场景？",
        "evidence_ids": ["E-0001"],
        "source_ref": f"Work/runs/{RUN}/reviews/interface-closures-r0.json",
    }
    answered = CrossDecisionIFClosure(
        **base,
        status="answered",
        answer="已覆盖当前整定版本。",
        conditions=["以当前整定版本为准"],
    )
    assert answered.status == "answered"
    with pytest.raises(ValidationError, match="literal|valid"):
        CrossDecisionIFClosure(**base, status="pending_cross")
    with pytest.raises(ValidationError):
        CrossDecisionIFClosure(
            **{**base, "evidence_ids": []},
            status="answered",
            answer="已覆盖。",
            conditions=["当前版本"],
        )


def test_view_retains_refs_and_rejects_foreign_artifact() -> None:
    payload = _pack_payload()
    payload.pop("artifact_sha256")
    payload.pop("pack_sha256")
    view = CrossDecisionPackView.model_validate(payload)
    assert view.artifact_refs == [COMPLETION]
    with pytest.raises(ValidationError, match="current run"):
        CrossDecisionPackView.model_validate(
            {
                **payload,
                "cross_review_completion_ref": "Work/runs/foreign/reviews/cross.json",
                "artifact_refs": ["Work/runs/foreign/reviews/cross.json"],
            }
        )


def test_final_finding_rejects_chapter_two_and_four() -> None:
    base = {
        "id": "F-001",
        "target_section_ids": ["3.2"],
        "target_changes": [
            {
                "target_section_id": "3.2",
                "required_change": "在本节补充责任接口、依赖顺序、联合验收指标和剩余风险边界。",
                "reviewer_checks": ["责任和验收均可核对"],
            }
        ],
        "category": "synthesis",
        "impact": "blocking",
        "observation": "行动计划没有说明跨模块责任接口及验收边界。",
        "evidence_refs": [COMPLETION],
    }
    assert FinalReviewFinding.model_validate(base).target_section_ids == ["3.2"]
    for section_id in ("2.1", "4"):
        with pytest.raises(ValidationError, match="invalid section"):
            FinalReviewFinding.model_validate(
                {
                    **base,
                    "target_section_ids": [section_id],
                    "target_changes": [
                        {**base["target_changes"][0], "target_section_id": section_id}
                    ],
                }
            )


def test_schema_examples_stay_valid_after_pack_split() -> None:
    for kind, model in INPUT_CONTRACT_TYPES.items():
        model.model_validate(INPUT_CONTRACT_EXAMPLES[kind])

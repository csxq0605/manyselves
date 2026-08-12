from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from manyselves.core.reporting.agentic_models import (
    SUBMISSION_INPUT_TYPES,
    CrossReviewFindingSubmission,
    CrossSynthesisInput,
    EditedReportSubmission,
    FinalReviewFinding,
    FinalReviewFindingSubmission,
    MachineCheckSpec,
    ModuleRevisionSubmission,
    ModuleSubmission,
    ResolutionVerdict,
    RevisionResponse,
    TaskEnvelope,
    WorkflowDecisionSubmission,
)
from manyselves.core.reporting.input_contracts import (
    INPUT_CONTRACT_EXAMPLES,
    INPUT_CONTRACT_TYPES,
    input_contract_schema,
)
from manyselves.core.reporting.submission_contracts import (
    KIND_EXAMPLES,
    submission_model,
    submission_schema,
    undescribed_property_paths,
)
from manyselves.core.reporting.taxonomy import REPORT_TAXONOMY


def _module(module_id: str = "2.1", revision: int = 0) -> ModuleSubmission:
    return ModuleSubmission(
        module_id=module_id,
        submodule_narratives={
            submodule_id: f"### {submodule_id}\n\n示例正文"
            for submodule_id in REPORT_TAXONOMY[module_id].submodules
        },
        claims=[],
        source_ids=[],
        unresolved_questions=[],
        revision=revision,
    )


def test_field_equals_requires_one_expected_value_per_path() -> None:
    with pytest.raises(ValidationError, match="one expected value per target path"):
        MachineCheckSpec(
            kind="field_equals",
            target_paths=["submodule_narratives.2.4.1.1"],
            expected_values=["96.99", "3500"],
        )

    check = MachineCheckSpec(
        kind="required_terms_present",
        target_paths=["submodule_narratives.2.4.1.1"],
        expected_values=["96.99", "3500"],
    )
    assert check.kind == "required_terms_present"


def test_task_envelope_requires_a_role_labelled_input_contract_ref() -> None:
    with pytest.raises(ValidationError, match="provided together"):
        TaskEnvelope(
            task_id="review",
            run_id="run-1",
            agent_id="evidence-auditor",
            objective="审查",
            input_contract_kind="module_review_input",
        )
    with pytest.raises(ValidationError, match="must also appear"):
        TaskEnvelope(
            task_id="review",
            run_id="run-1",
            agent_id="evidence-auditor",
            objective="审查",
            input_contract_kind="module_review_input",
            input_contract_ref="reviews/input.json",
        )


def test_module_submission_derives_markdown_from_fixed_submodules() -> None:
    module = _module()
    assert module.markdown
    assert set(module.submodule_narratives) == set(REPORT_TAXONOMY["2.1"].submodules)


def test_module_revision_is_an_explicit_patch_not_a_partial_module() -> None:
    target = next(iter(REPORT_TAXONOMY["2.1"].submodules))
    patch = ModuleRevisionSubmission(
        module_id="2.1",
        base_revision=0,
        revision=1,
        submodule_narratives={target: "### 修订\n\n明确改动"},
        claims_upsert=[],
        claim_ids_remove=[],
        source_ids=[],
        unresolved_questions=[],
        revision_responses=[
            RevisionResponse(
                finding_id="M-001",
                action="implemented",
                summary="已在指定小节完成结构化的定向修改并保留证据边界。",
                changed_target_ids=[target],
            )
        ],
    )
    assert set(patch.submodule_narratives) == {target}
    assert not hasattr(patch, "markdown")


def test_module_revision_rejects_undeclared_or_mismatched_changes() -> None:
    target = next(iter(REPORT_TAXONOMY["2.1"].submodules))
    with pytest.raises(ValidationError, match="must equal the targets"):
        ModuleRevisionSubmission(
            module_id="2.1",
            base_revision=0,
            revision=1,
            submodule_narratives={target: "修改"},
            claims_upsert=[],
            claim_ids_remove=[],
            source_ids=[],
            unresolved_questions=[],
            revision_responses=[
                RevisionResponse(
                    finding_id="M-001",
                    action="disputed",
                    summary="当前证据不支持修改，因此明确提出异议并请求原审查者复核。",
                    changed_target_ids=[],
                )
            ],
        )


def test_resolved_verdict_requires_current_evidence() -> None:
    with pytest.raises(ValidationError, match="requires current evidence"):
        ResolutionVerdict(
            finding_id="M-001",
            verdict="resolved",
            reason="已经核对当前正文并判断问题关闭，但这里故意没有提供证据引用。",
            evidence_refs=[],
        )


def test_cross_coverage_contains_each_module_exactly_once() -> None:
    with pytest.raises(ValidationError, match="each module exactly once"):
        CrossReviewFindingSubmission(
            coverage=[
                {
                    "module_id": "2.1",
                    "checked_dimensions": ["terminology"],
                }
            ]
            * 5,
            findings=[],
            synthesis_inputs=[],
        )


def test_cross_synthesis_rejects_module_refs_missing_from_declared_scope() -> None:
    with pytest.raises(ValidationError, match="undeclared related modules"):
        CrossSynthesisInput(
            id="SI-003",
            related_module_ids=["2.2", "2.3", "2.5"],
            cluster_type="monitoring_blind_spot",
            root_causes=["2.2、2.3 与 2.5 的监测接口尚未形成闭环"],
            propagation_steps=[
                "2.2 异常信号未形成稳定输入",
                "2.4.3.1 的处置接口因此无法及时触发",
            ],
            causal_chain=(
                "2.2 的异常信号和 2.3 的保护状态未能传递到 2.4.3.1 的现场处置及 2.5 的管理闭环。"
            ),
            decision_implication="必须先补齐监测接口，再调整巡检和应急响应顺序。",
            action_dependencies=["先完成信号核对，再更新现场处置和管理流程"],
            joint_actions=["联合完成信号、处置与管理闭环验证"],
            verification_method="通过事件注入、告警记录和处置时间联合验证。",
            acceptance_criteria=["告警、处置和关闭记录可以完整追溯"],
            module_statement_refs=["2.2.1", "2.3.1", "2.5.1"],
            confidence_and_boundary="当前仅确认接口关系，具体阈值仍需现场数据复核。",
            target_report_section_ids=["3.1.2", "3.2"],
            evidence_refs=["Work/runs/example/modules/2.2-r0.json"],
        )


def test_edited_report_drops_deleted_cross_module_metadata_from_old_checkpoints() -> None:
    payload = {
        "title": "报告",
        "assessment_background": "背景",
        "findings_overview": "发现",
        "regional_executive_summary": "摘要",
        "module_narratives": {
            module_id: f"{module_id} 已批准正文" for module_id in REPORT_TAXONOMY
        },
        "risk_panorama": "风险全景",
        "dimension_risk_analysis": "维度风险分析",
        "data_gap_analysis": "数据缺口分析",
        "improvement_action_plan": "改善行动",
        "synthesis_dispositions": [],
        "synthesis_tables": [],
    }

    edited = EditedReportSubmission.model_validate(payload)

    assert "synthesis_dispositions" not in edited.model_dump()
    assert "synthesis_tables" not in edited.model_dump()


def test_final_review_rejects_actionable_report_defect_as_residual_risk() -> None:
    with pytest.raises(ValidationError, match="must be final-review findings"):
        FinalReviewFindingSubmission(
            checked_section_ids=["1.1"],
            findings=[],
            residual_risks=["报告缺少 Cross 风险综合表格"],
        )


def test_final_finding_rejects_chapter_two_and_requires_per_target_changes() -> None:
    base = {
        "id": "F-001",
        "target_section_ids": ["3.1.2"],
        "target_changes": [
            {
                "target_section_id": "3.1.2",
                "required_change": "在该小节补充行动依赖顺序和可验证的联合验收方法。",
                "reviewer_checks": ["行动依赖、责任接口和联合验收均可核对"],
            }
        ],
        "category": "synthesis",
        "impact": "blocking",
        "observation": "当前维度风险分析没有清楚说明各维度判断及其管理含义。",
        "evidence_refs": ["Work/runs/run-1/edited-revisions/chief-r0.json"],
    }
    assert FinalReviewFinding.model_validate(base).target_section_ids == ["3.1.2"]

    with pytest.raises(ValidationError, match="invalid section"):
        FinalReviewFinding.model_validate(
            {
                **base,
                "target_section_ids": ["2.4"],
                "target_changes": [
                    {
                        **base["target_changes"][0],
                        "target_section_id": "2.4",
                    }
                ],
            }
        )
    with pytest.raises(ValidationError, match="exactly match"):
        FinalReviewFinding.model_validate(
            {
                **base,
                "target_section_ids": ["3.1.2", "3.2"],
            }
        )


def test_main_decision_contract_is_exception_only_and_has_no_waiver() -> None:
    decision = WorkflowDecisionSubmission(
        decision="accept_dispute",
        rationale="已检查被升级的 finding 与当前正文，接受作者的证据化异议。",
        finding_ids=["X-001"],
    )
    assert decision.finding_ids == ["X-001"]
    assert "waived_issue_ids" not in type(decision).model_fields
    with pytest.raises(ValidationError):
        WorkflowDecisionSubmission(
            decision="accept",
            rationale="旧决定值不再允许。",
            finding_ids=["X-001"],
        )


def test_every_model_output_contract_has_semantics_and_a_valid_example() -> None:
    assert set(KIND_EXAMPLES) == set(SUBMISSION_INPUT_TYPES)
    for kind, example in KIND_EXAMPLES.items():
        submission_model(kind).model_validate(example)
        assert undescribed_property_paths(submission_schema(kind)) == []


def test_every_model_input_contract_has_semantics_and_a_valid_example() -> None:
    assert set(INPUT_CONTRACT_EXAMPLES) == set(INPUT_CONTRACT_TYPES)
    for kind, model in INPUT_CONTRACT_TYPES.items():
        model.model_validate(INPUT_CONTRACT_EXAMPLES[kind])
        assert undescribed_property_paths(input_contract_schema(kind)) == []


def test_model_input_contracts_hide_runtime_claim_protocol() -> None:
    forbidden = (
        "protected_claim_ids",
        "affected_claim_ids",
        "claim_ids",
        "claims_upsert",
        "claim_ids_remove",
        "[[CLAIM:",
        "C-*",
    )
    for kind in INPUT_CONTRACT_TYPES:
        rendered = json.dumps(input_contract_schema(kind), ensure_ascii=False)
        for item in forbidden:
            assert item not in rendered


def test_old_review_submission_kinds_are_absent() -> None:
    for kind in (
        "audit_submission",
        "cross_review_submission",
        "final_audit_submission",
    ):
        with pytest.raises(KeyError):
            submission_model(kind)

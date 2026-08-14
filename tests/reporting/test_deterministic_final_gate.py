from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from manyselves.core.reporting.agentic_models import (
    FINAL_AUDIT_SECTION_IDS,
)
from manyselves.core.reporting.assets import validate_final_report_markdown
from manyselves.core.reporting.input_contracts import (
    CrossDecisionPackView,
    FinalAuditMetadataView,
    FinalReviewInput,
    ValidationReport,
)
from manyselves.core.reporting.models import SpecialTopicPlan
from manyselves.core.reporting.report_markdown import (
    CanonicalReportContent,
    compose_canonical_markdown,
)
from manyselves.core.reporting.review_lifecycle import _final_audit_markdown
from manyselves.core.reporting.taxonomy import REPORT_TAXONOMY, compose_module_markdown


RUN_ID = "run-deterministic-final-gate"


def _body(label: str) -> str:
    return (
        f"{label} 已核查项目记录、运行数据和现场边界，当前条件可能导致风险沿上下游传播，"
        "并在负荷变化、保护配合或维护不足时进一步放大。建议由责任部门核验关键假设，"
        "制定分阶段整改动作，完成复测并以验收记录关闭风险；证据不足处保留待补充边界。"
    ) * 2


def _modules() -> dict[str, str]:
    return {
        module_id: compose_module_markdown(
            module_id,
            {
                submodule_id: _body(submodule_id)
                for submodule_id in definition.submodules
            },
        )
        for module_id, definition in REPORT_TAXONOMY.items()
    }


def _plan() -> SpecialTopicPlan:
    return SpecialTopicPlan(
        source_ref=Path("Inputs/专项问题分析.md"),
        source_sha256="0" * 64,
        sections=[
            {
                "section_id": "4.1",
                "title": "动态专项问题",
                "requirement": "说明专项边界、方案比较和验证方法。",
            }
        ],
    )


def _markdown(plan: SpecialTopicPlan | None) -> str:
    return compose_canonical_markdown(
        CanonicalReportContent(
            title="配电安全专家咨询报告",
            assessment_background=_body("评估背景"),
            findings_overview=_body("健康度总览"),
            regional_executive_summary=_body("区域摘要"),
            module_narratives=_modules(),
            risk_panorama=_body("风险全景"),
            dimension_risk_analysis=_body("维度分析"),
            data_gap_analysis=_body("数据缺口"),
            improvement_action_plan=_body("行动计划"),
            special_topic_plan=plan,
            special_topic_analysis=(
                "### 4.1 动态专项问题\n\n" + _body("专项问题") if plan else None
            ),
        )
    )


def _cross_view() -> CrossDecisionPackView:
    completion = f"Work/runs/{RUN_ID}/reviews/cross-completion.json"
    return CrossDecisionPackView(
        run_id=RUN_ID,
        module_ids=["2.1", "2.2", "2.3", "2.4", "2.5"],
        cross_review_completion_ref=completion,
        artifact_refs=[completion],
    )


def _validation(subject_revision: int = 0) -> ValidationReport:
    return ValidationReport(
        validation_protocol_version=2,
        run_id=RUN_ID,
        subject_ref=f"Work/runs/{RUN_ID}/edited-revisions/chief-r{subject_revision}.json",
        subject_revision=subject_revision,
        content_sha256="0" * 64,
        validator="final-report-structure/v2",
        check_ids=["final_report.fixed_sections_and_markdown"],
        passed=True,
    )


def test_structure_gate_requires_exact_order_levels_and_unique_headings() -> None:
    valid = _markdown(_plan())
    validate_final_report_markdown(valid, _plan())

    duplicate = valid.replace(
        "### 1.1 评估背景",
        "### 1.1 评估背景\n\n### 1.1 评估背景",
        1,
    )
    with pytest.raises(ValueError, match="heading_errors"):
        validate_final_report_markdown(duplicate, _plan())

    wrong_level = valid.replace("### 1.1 评估背景", "## 1.1 评估背景", 1)
    with pytest.raises(ValueError, match="heading_level"):
        validate_final_report_markdown(wrong_level, _plan())

    reordered = valid.replace(
        "### 1.1 评估背景",
        "### TEMP 评估背景",
        1,
    ).replace(
        "### 1.2 健康度总览",
        "### 1.1 评估背景",
        1,
    ).replace("### TEMP 评估背景", "### 1.2 健康度总览", 1)
    with pytest.raises(ValueError, match="fixed headings are out of order"):
        validate_final_report_markdown(reordered, _plan())


def test_structure_gate_binds_chapter_four_to_special_topic_plan() -> None:
    without_plan = _markdown(None)
    validate_final_report_markdown(without_plan, None)
    with pytest.raises(ValueError, match="Chapter 4 must be absent"):
        validate_final_report_markdown(
            without_plan + "\n## 4. 专项问题分析\n\n" + _body("擅自专项"),
            None,
        )

    with_plan = _markdown(_plan())
    validate_final_report_markdown(with_plan, _plan())
    missing_chapter = with_plan.replace("## 4. 专项问题分析\n\n", "", 1)
    with pytest.raises(ValueError, match=r"4\. 专项问题分析: count=0"):
        validate_final_report_markdown(missing_chapter, _plan())


def test_structure_gate_allows_chapter_four_descendants_and_counts_internal_labels() -> None:
    plan = _plan()
    markdown = _markdown(plan)
    markdown = markdown.replace(
        _body("维度分析"),
        "### 维度一：热应力与容量裕度\n\n" + _body("维度分析"),
        1,
    ).replace(
        "### 4.1 动态专项问题\n\n" + _body("专项问题"),
        "### 4.1 动态专项问题\n\n"
        + _body("专项问题")
        + "\n\n#### 4.1.1 验证步骤\n\n"
        + _body("专项验证"),
        1,
    )

    signals = validate_final_report_markdown(markdown, plan)

    assert "3.1.2 各维度风险分析" not in signals["shallow_sections"]
    assert "4.1 动态专项问题" not in signals["shallow_sections"]

    with pytest.raises(ValueError, match="headings must exactly match"):
        validate_final_report_markdown(
            markdown
            + "\n\n### 4.2 计划外顶层小节\n\n"
            + _body("计划外专项"),
            plan,
        )


def test_final_review_contract_remains_seven_sections_even_when_chapter_four_exists() -> None:
    contract = FinalReviewInput(
        phase="initial",
        run_id=RUN_ID,
        cross_decision=_cross_view(),
        cross_decision_pack_ref=f"Work/runs/{RUN_ID}/reviews/cross-decision-pack.json",
        subject_ref=f"Work/runs/{RUN_ID}/edited-revisions/chief-r0.json",
        subject_revision=0,
        subject_metadata=FinalAuditMetadataView(),
        # Final semantics receive only the 1.x/3.x projection.  The full
        # Chapter 4 variant is validated above by the deterministic gate and
        # is never admitted to this contract.
        canonical_markdown=_final_audit_markdown(_markdown(None)),
        required_section_ids=list(FINAL_AUDIT_SECTION_IDS),
        validation_report_ref=f"Work/runs/{RUN_ID}/reviews/validation.json",
        validation_report=_validation(),
    )
    assert set(contract.required_section_ids) == set(FINAL_AUDIT_SECTION_IDS)
    invalid_payload = contract.model_dump(mode="python")
    invalid_payload["required_section_ids"] = [*FINAL_AUDIT_SECTION_IDS, "4.1"]
    with pytest.raises(ValidationError, match="seven summary/conclusion"):
        FinalReviewInput.model_validate(invalid_payload)

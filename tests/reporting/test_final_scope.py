from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from manyselves.capabilities.distribution_reporting.runtime.models.agentic import (
    FINAL_AUDIT_SECTION_IDS,
    FinalReviewFinding,
)
from manyselves.capabilities.distribution_reporting.runtime.models.reporting import (
    CHIEF_SECTION_RESULT_PART_IDS,
    FINAL_SUMMARY_CONCLUSION_AUDIT_SECTION_IDS,
)
from manyselves.core.reporting.input_contracts import (
    AggregateFinalReviewInput,
    CrossDecisionPackView,
    FinalAuditMetadataView,
    FinalReviewInput,
    RevisionResponse,
    ValidationReport,
)
from manyselves.core.reporting.review_lifecycle import (
    _final_audit_markdown,
    _final_audit_section_bodies,
)

RUN = "run-final-scope"
COMPLETION = f"Work/runs/{RUN}/reviews/cross-completion.json"


def _validation(subject_revision: int = 0) -> ValidationReport:
    return ValidationReport(
        validation_protocol_version=2,
        run_id=RUN,
        subject_ref=f"Work/runs/{RUN}/edited-revisions/chief-r{subject_revision}.json",
        subject_revision=subject_revision,
        validator="final-report-structure/v2",
        check_ids=["final_report.fixed_sections_and_markdown"],
        failures=[],
        passed=True,
    )


def _cross_view() -> CrossDecisionPackView:
    return CrossDecisionPackView(
        run_id=RUN,
        module_ids=["2.1", "2.2", "2.3", "2.4", "2.5"],
        cross_review_completion_ref=COMPLETION,
        artifact_refs=[COMPLETION],
    )


def _finding(section_id: str = "3.2") -> FinalReviewFinding:
    return FinalReviewFinding(
        id="F-001",
        target_section_ids=[section_id],
        target_changes=[
            {
                "target_section_id": section_id,
                "required_change": "补充责任接口、验收指标、依赖顺序和剩余风险边界。",
                "reviewer_checks": ["责任和验收均可核对"],
            }
        ],
        category="synthesis",
        impact="blocking",
        observation="当前摘要缺少可执行的责任与验收边界，读者无法据此排序行动。",
        evidence_refs=[COMPLETION],
    )


def test_final_scope_is_exactly_seven_and_markdown_excludes_chapters_two_four() -> None:
    assert FINAL_AUDIT_SECTION_IDS == (
        "1.1",
        "1.2",
        "1.3",
        "3.1.1",
        "3.1.2",
        "3.1.3",
        "3.2",
    )
    markdown = (
        "# 报告\n\n## 1. 配电评估概述\n\n摘要\n\n"
        "## 2. 评估内容描述\n\n模块全文\n\n"
        "## 3. 结论与建议\n\n结论\n\n"
        "## 4. 专项问题分析\n\n专项全文\n"
    )
    audited = _final_audit_markdown(markdown)
    assert "## 2." not in audited
    assert "## 4." not in audited
    assert "## 3." in audited

    subject = SimpleNamespace(
        **{
            field_name: f"正文-{section_id}"
            for section_id, field_name in CHIEF_SECTION_RESULT_PART_IDS.items()
        },
        special_topic_plan=object(),
    )
    assert tuple(_final_audit_section_bodies(subject)) == (
        FINAL_SUMMARY_CONCLUSION_AUDIT_SECTION_IDS
    )
    assert "4" not in _final_audit_section_bodies(subject)


def test_aggregate_final_adapter_is_explicit_and_has_no_cross_pack() -> None:
    contract = AggregateFinalReviewInput(
        phase="initial",
        run_id=RUN,
        cross_context=None,
        subject_ref=f"Work/runs/{RUN}/edited-revisions/chief-r0.json",
        subject_revision=0,
        subject_metadata=FinalAuditMetadataView(),
        canonical_markdown="# 报告\n\n七节摘要",
        required_section_ids=list(FINAL_AUDIT_SECTION_IDS),
        validation_report_ref=f"Work/runs/{RUN}/reviews/validation.json",
        validation_report=_validation(),
    )
    assert contract.mode == "aggregate_existing"
    assert contract.cross_context is None
    assert "cross_decision_pack_ref" not in contract.model_dump()


def test_final_recheck_contains_only_changed_bodies_and_other_section_hashes() -> None:
    finding = _finding()
    response = RevisionResponse(
        finding_id=finding.id,
        action="implemented",
        summary="已补充责任接口、依赖顺序、验收指标和剩余风险边界，供审计逐项核对。",
        changed_target_ids=["3.2"],
    )
    contract = FinalReviewInput(
        phase="recheck",
        run_id=RUN,
        cross_decision=_cross_view(),
        cross_decision_pack_ref=f"Work/runs/{RUN}/reviews/cross-decision-pack.json",
        subject_ref=f"Work/runs/{RUN}/edited-revisions/chief-r1.json",
        subject_revision=1,
        subject_metadata_sha256="1" * 64,
        changed_section_bodies={"3.2": "修订后的行动计划摘要"},
        unchanged_section_sha256={
            section_id: "2" * 64
            for section_id in FINAL_AUDIT_SECTION_IDS
            if section_id != "3.2"
        },
        required_section_ids=list(FINAL_AUDIT_SECTION_IDS),
        required_findings=[finding],
        revision_responses=[response],
        validation_report_ref=f"Work/runs/{RUN}/reviews/validation-r1.json",
        validation_report=_validation(1),
    )
    assert set(contract.changed_section_bodies) == {"3.2"}
    assert set(contract.unchanged_section_sha256) == set(FINAL_AUDIT_SECTION_IDS) - {"3.2"}
    with pytest.raises(ValidationError):
        _finding("4")

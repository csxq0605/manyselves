from pathlib import Path

import pytest
from pydantic import ValidationError

from manyselves.core.reporting.models import (
    CoverageEntry,
    CoverageMatrix,
    CoverageStatus,
    EvidenceItem,
    ReportRequest,
    RevisionRequest,
    SourceLocation,
    UserSupplement,
)
from manyselves.core.reporting.workflow import ReportWorkflowRunner


def test_report_request_keeps_run_requirements_separate_from_scope() -> None:
    request = ReportRequest(
        operation="module_report",
        instruction="生成配电房现状分析",
        target_modules=["2.4"],
        execution_requirements=["deep_reasoning"],
    )

    assert request.target_modules == ["2.4"]
    assert request.operation == "module_report"
    assert request.execution_requirements == ["deep_reasoning"]
    assert request.missing_evidence_policy == "draft"
    draft_constraints = ReportWorkflowRunner._evidence_policy_constraints("draft")
    assert "资料不完整、待核实、低置信度" in draft_constraints[0]
    assert "不得因此跳过固定模块或子模块" in draft_constraints[0]


def test_render_existing_requires_project_relative_markdown() -> None:
    request = ReportRequest(
        operation="render_existing",
        instruction="转换为 Word",
        source_markdown_ref=Path("Work/drafts/report.md"),
        output_filename="report.docx",
    )

    assert request.operation == "render_existing"
    assert request.source_markdown_ref == Path("Work/drafts/report.md")

    with pytest.raises(ValidationError, match="source_markdown_ref"):
        ReportRequest(operation="render_existing", instruction="转换为 Word")
    with pytest.raises(ValidationError, match="inside"):
        ReportRequest(
            operation="render_existing",
            instruction="转换为 Word",
            source_markdown_ref=Path("../outside.md"),
        )


def test_report_request_enforces_five_explicit_route_contracts() -> None:
    distill = ReportRequest(
        operation="distill_template_skill",
        instruction="蒸馏模板写作 Skill",
        target_modules=[],
    )
    full = ReportRequest(operation="full_report", instruction="从原始资料生成完整报告")
    partial = ReportRequest(
        operation="module_report",
        instruction="只生成 2.4",
        target_modules=["2.4"],
    )
    aggregate = ReportRequest(
        operation="aggregate_existing",
        instruction="汇总已有五份模块报告",
    )

    assert distill.target_modules == []
    assert full.operation == "full_report"
    assert partial.target_modules == ["2.4"]
    assert aggregate.source_module_refs is None

    with pytest.raises(ValidationError, match="exactly modules"):
        ReportRequest(
            operation="full_report",
            instruction="错误地只生成 2.4",
            target_modules=["2.4"],
        )
    with pytest.raises(ValidationError, match="does not accept target modules"):
        ReportRequest(
            operation="distill_template_skill",
            instruction="错误地携带模块",
            target_modules=["2.4"],
        )
    with pytest.raises(ValidationError, match="only valid for aggregate_existing"):
        ReportRequest(
            operation="module_report",
            instruction="错误输入",
            target_modules=["2.4"],
            source_module_refs={"2.4": Path("Outputs/Modules/2.4.md")},
        )


def test_report_request_rejects_invalid_run_budget() -> None:
    with pytest.raises(ValidationError):
        ReportRequest(instruction="生成 2.4", max_provider_attempts=0)
    with pytest.raises(ValidationError):
        ReportRequest(instruction="生成 2.4", max_total_tokens=999)


def test_report_request_defaults_to_all_ready_and_keeps_legacy_modes() -> None:
    request = ReportRequest(
        operation="full_report",
        instruction="生成完整报告",
    )
    bounded = request.model_copy(
        update={"execution_mode": "bounded_module_lanes"}
    )

    assert request.execution_mode == "all_ready"
    assert bounded.execution_mode == "bounded_module_lanes"
    serial = request.model_copy(update={"execution_mode": "current_serial_review"})
    assert serial.execution_mode == "current_serial_review"
    assert (
        ReportRequest.model_json_schema()["properties"]["execution_mode"]["default"]
        == "all_ready"
    )


def test_typed_supplements_filter_by_stage_scope_and_supersession() -> None:
    request = ReportRequest(
        operation="module_report",
        instruction="生成 2.4",
        target_modules=["2.4"],
        user_supplements=[
            UserSupplement(
                id="US-old",
                content="旧设备名称为嘉仕工厂。",
                scope="module",
                target_ids=["2.4"],
                stages=["module_authoring", "module_review"],
            ),
            UserSupplement(
                id="US-current",
                content="确认统一使用芜湖工厂。",
                scope="module",
                target_ids=["2.4"],
                stages=["module_authoring", "module_review"],
                supersedes=["US-old"],
            ),
            UserSupplement(
                id="US-final",
                content="最终报告需要突出管理责任。",
                scope="final_section",
                target_ids=["3.2"],
                stages=["chief_edit", "final_review"],
            ),
        ],
    )

    constraints = ReportWorkflowRunner._user_supplement_constraints(
        {"request": request},
        stage="module_authoring",
        target_ids={"2.4"},
    )

    assert len(constraints) == 1
    assert "US-current" in constraints[0]
    assert "US-old" not in constraints[0]
    assert "US-final" not in constraints[0]


def test_typed_supplement_requires_targets_outside_run_scope() -> None:
    with pytest.raises(ValidationError, match="requires target_ids"):
        UserSupplement(
            id="US-invalid",
            content="只影响一个模块。",
            scope="module",
        )


def test_typed_supplement_rejects_unknown_final_section() -> None:
    with pytest.raises(ValidationError, match="final-section targets are invalid"):
        UserSupplement(
            id="US-invalid-section",
            content="只影响不存在的最终章节。",
            scope="final_section",
            target_ids=["9.9"],
        )


def test_revision_request_rejects_invalid_run_budget() -> None:
    with pytest.raises(ValidationError):
        RevisionRequest(
            baseline_version_id="version-001",
            feedback="修订 2.4",
            target_module_ids=["2.4"],
            max_provider_attempts=0,
        )
    with pytest.raises(ValidationError):
        RevisionRequest(
            baseline_version_id="version-001",
            feedback="修订 2.4",
            target_module_ids=["2.4"],
            max_total_tokens=999,
        )


def test_evidence_requires_a_traceable_source_location() -> None:
    with pytest.raises(ValidationError, match="path"):
        EvidenceItem(
            id="ev-1",
            subject="1号进线柜",
            fact="温度为 80 摄氏度",
            source=SourceLocation(file_id="file-1", path=Path("")),
        )


def test_coverage_matrix_rejects_unknown_report_module() -> None:
    with pytest.raises(ValidationError, match="2.1"):
        CoverageMatrix(
            entries={
                "3.1": CoverageEntry(
                    module_id="3.1",
                    status=CoverageStatus.READY,
                    evidence_ids=["ev-1"],
                )
            }
        )


def test_evidence_submodule_must_belong_to_declared_module() -> None:
    with pytest.raises(ValidationError, match="does not belong"):
        EvidenceItem(
            id="ev-2",
            subject="1号进线柜",
            fact="接地连接缺失",
            source=SourceLocation(
                file_id="file-1",
                path=Path("Inputs/S4-4诊断工作用表.xlsx"),
                sheet="低配评估详情",
                cell="M5",
                row=5,
                column="M",
            ),
            module_id="2.3",
            submodule_id="2.4.2.2",
            photo_refs=["ID_EXAMPLE"],
        )

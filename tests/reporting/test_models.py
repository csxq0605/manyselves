from pathlib import Path

import pytest
from pydantic import ValidationError

from autoreport.core.reporting.models import (
    Claim,
    ClaimKind,
    CoverageEntry,
    CoverageMatrix,
    CoverageStatus,
    EvidenceItem,
    ReportRequest,
    SourceLocation,
)


def test_report_request_keeps_run_requirements_separate_from_scope() -> None:
    request = ReportRequest(
        instruction="生成配电房现状分析",
        target_modules=["2.4"],
        execution_requirements=["deep_reasoning"],
    )

    assert request.target_modules == ["2.4"]
    assert request.execution_requirements == ["deep_reasoning"]


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


def test_quantitative_claim_requires_traceable_evidence() -> None:
    with pytest.raises(ValidationError, match="evidence"):
        Claim(
            id="claim-1",
            module_id="2.4",
            submodule_id="2.4.3.1",
            kind=ClaimKind.CONCLUSION,
            text="车间配电房 1A2 剩余电流为 46.8A。",
            evidence_ids=[],
        )


def test_unverified_claim_may_record_missing_evidence_explicitly() -> None:
    claim = Claim(
        id="claim-2",
        module_id="2.4",
        submodule_id="2.4.3.2",
        kind=ClaimKind.CONCLUSION,
        text="高压柜照明状态待核实。",
        evidence_ids=[],
        unverified=True,
    )

    assert claim.unverified is True


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

from pathlib import Path

import pytest
from pydantic import ValidationError

from pds_report.domain.models import (
    CoverageEntry,
    CoverageMatrix,
    CoverageStatus,
    EvidenceItem,
    ReportRequest,
    SourceLocation,
)


def test_report_request_keeps_execution_requirements_separate() -> None:
    request = ReportRequest(
        task="write_report",
        target_modules=["2.4"],
        execution_requirements=["deep_reasoning"],
    )

    assert request.task == "write_report"
    assert request.target_modules == ["2.4"]
    assert request.execution_requirements == ["deep_reasoning"]


def test_report_request_rejects_unknown_module() -> None:
    with pytest.raises(ValidationError, match="target_modules"):
        ReportRequest(task="write_report", target_modules=["3.1"])


def test_evidence_item_requires_source_location() -> None:
    with pytest.raises(ValidationError, match="source"):
        EvidenceItem(id="ev-1", fact="主柜温度为 80°C")


def test_evidence_item_keeps_openable_relative_source() -> None:
    evidence = EvidenceItem(
        id="ev-1",
        fact="主柜温度为 80°C",
        object="主进线柜",
        value="80",
        unit="°C",
        source=SourceLocation(
            file_id="file-1",
            relative_path=Path("Inputs/巡检记录.md"),
            locator="第 3 行",
        ),
    )

    assert evidence.source.relative_path == Path("Inputs/巡检记录.md")
    assert evidence.value == "80"


def test_coverage_matrix_uses_explicit_statuses() -> None:
    matrix = CoverageMatrix(
        entries=[
            CoverageEntry(
                module_id="2.4",
                status=CoverageStatus.READY,
                evidence_ids=["ev-1"],
            )
        ]
    )

    assert matrix.entries[0].status is CoverageStatus.READY


def test_models_forbid_unknown_fields() -> None:
    with pytest.raises(ValidationError, match="extra_forbidden"):
        ReportRequest(task="write_report", target_modules=["2.4"], surprise=True)

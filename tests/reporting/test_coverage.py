from pathlib import Path

from manyselves.core.reporting.coverage import evaluate_coverage
from manyselves.core.reporting.models import (
    CoverageStatus,
    EvidenceItem,
    ReportRequest,
    SourceLocation,
)


def _evidence(
    evidence_id: str,
    module_id: str,
    submodule_id: str,
) -> EvidenceItem:
    return EvidenceItem(
        id=evidence_id,
        subject="测试设备",
        fact="测试事实",
        source=SourceLocation(file_id="file-1", path=Path("Inputs/test.xlsx")),
        module_id=module_id,
        submodule_id=submodule_id,
    )


def test_unrelated_evidence_cannot_make_module_24_ready() -> None:
    request = ReportRequest(
        operation="module_report",
        instruction="生成 2.4",
        target_modules=["2.4"],
    )
    unrelated = _evidence("ev-21", "2.1", "2.1.1")

    coverage = evaluate_coverage(request, [unrelated])

    module = coverage.entries["2.4"]
    assert module.status is CoverageStatus.PENDING
    assert module.evidence_ids == []
    assert all(entry.status is CoverageStatus.PENDING for entry in module.submodules.values())


def test_coverage_marks_only_exact_submodule_ready() -> None:
    request = ReportRequest(
        operation="module_report",
        instruction="生成 2.4",
        target_modules=["2.4"],
    )
    grounding = _evidence("ev-ground", "2.4", "2.4.2.2")

    coverage = evaluate_coverage(request, [grounding])

    module = coverage.entries["2.4"]
    assert module.submodules["2.4.2.2"].status is CoverageStatus.READY
    assert module.submodules["2.4.2.2"].evidence_ids == ["ev-ground"]
    assert module.submodules["2.4.2.1"].status is CoverageStatus.PENDING
    assert module.evidence_ids == ["ev-ground"]


def test_block_policy_marks_missing_submodules_blocked() -> None:
    request = ReportRequest(
        operation="module_report",
        instruction="生成 2.4",
        target_modules=["2.4"],
        missing_evidence_policy="block",
    )

    coverage = evaluate_coverage(request, [])

    assert coverage.entries["2.4"].status is CoverageStatus.BLOCKED
    assert coverage.entries["2.4"].submodules["2.4.1.1"].status is CoverageStatus.BLOCKED

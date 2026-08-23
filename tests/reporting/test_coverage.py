from pathlib import Path

from manyselves.capabilities.distribution_reporting.domain.coverage import evaluate_coverage
from manyselves.capabilities.distribution_reporting.runtime.models.reporting import (
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


def test_targeted_mapping_gap_prevents_false_ready_status() -> None:
    request = ReportRequest(
        operation="module_report",
        instruction="生成 2.4",
        target_modules=["2.4"],
    )
    partial = _evidence("ev-partial", "2.4", "2.4.4")

    coverage = evaluate_coverage(
        request,
        [partial],
        mapping_gaps=[
            {
                "code": "incomplete_thermal_context",
                "message": "红外实测温度缺少配电房或柜号上下文",
                "module_id": "2.4",
                "submodule_id": "2.4.4",
            }
        ],
    )

    thermal = coverage.entries["2.4"].submodules["2.4.4"]
    assert thermal.status is CoverageStatus.PENDING
    assert thermal.evidence_ids == ["ev-partial"]
    assert thermal.gaps == ["红外实测温度缺少配电房或柜号上下文"]
    assert coverage.entries["2.4"].evidence_ids == ["ev-partial"]


def test_unclassified_mapping_gap_prevents_module_false_ready() -> None:
    request = ReportRequest(
        operation="module_report",
        instruction="生成 2.4",
        target_modules=["2.4"],
    )
    evidence = [
        _evidence(f"ev-{index}", "2.4", submodule_id)
        for index, submodule_id in enumerate(
            (
                "2.4.1.1",
                "2.4.1.2",
                "2.4.1.3",
                "2.4.1.4",
                "2.4.2.1",
                "2.4.2.2",
                "2.4.2.3",
                "2.4.2.4",
                "2.4.2.5",
                "2.4.2.6",
                "2.4.3.1",
                "2.4.3.2",
                "2.4.3.3",
                "2.4.4",
            ),
            start=1,
        )
    ]

    coverage = evaluate_coverage(
        request,
        evidence,
        mapping_gaps=[
            {
                "code": "unrouted_document",
                "message": "收资项未匹配报告子模块",
            }
        ],
    )

    module = coverage.entries["2.4"]
    assert all(entry.status is CoverageStatus.READY for entry in module.submodules.values())
    assert module.status is CoverageStatus.PENDING
    assert module.gaps == ["收资项未匹配报告子模块"]

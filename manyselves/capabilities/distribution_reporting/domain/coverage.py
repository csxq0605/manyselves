"""Deterministic evidence coverage evaluation for the fixed report taxonomy."""

from collections.abc import Mapping
from typing import Any

from manyselves.capabilities.distribution_reporting.domain.taxonomy import REPORT_TAXONOMY
from manyselves.capabilities.distribution_reporting.runtime.models.reporting import (
    REPORT_MODULE_IDS,
    CoverageEntry,
    CoverageMatrix,
    CoverageStatus,
    EvidenceItem,
    ReportRequest,
    SubmoduleCoverageEntry,
)


def _missing_status(request: ReportRequest) -> CoverageStatus:
    if request.missing_evidence_policy == "block":
        return CoverageStatus.BLOCKED
    return CoverageStatus.PENDING


def evaluate_coverage(
    request: ReportRequest,
    evidence_items: list[EvidenceItem],
    mapping_gaps: list[Mapping[str, Any]] | None = None,
) -> CoverageMatrix:
    """Evaluate requested submodules using classified evidence and extraction gaps."""

    mapping_gaps = mapping_gaps or []
    global_extraction_gaps = [
        str(gap.get("message") or gap.get("code") or gap.get("kind") or "证据提取不完整")
        for gap in mapping_gaps
        if not gap.get("module_id")
    ]
    entries: dict[str, CoverageEntry] = {}
    for module_id in REPORT_MODULE_IDS:
        definition = REPORT_TAXONOMY[module_id]
        requested = module_id in request.target_modules
        submodules: dict[str, SubmoduleCoverageEntry] = {}
        module_evidence_ids: list[str] = []
        module_gaps: list[str] = []
        module_extraction_gaps = [
            str(gap.get("message") or gap.get("code") or gap.get("kind") or "证据提取不完整")
            for gap in mapping_gaps
            if gap.get("module_id") == module_id and not gap.get("submodule_id")
        ]
        for submodule_id in definition.submodules:
            matched_ids = [
                item.id
                for item in evidence_items
                if item.module_id == module_id and item.submodule_id == submodule_id
            ]
            extraction_gaps = [
                str(gap.get("message") or gap.get("code") or "证据提取不完整")
                for gap in mapping_gaps
                if gap.get("module_id") == module_id
                and gap.get("submodule_id") == submodule_id
            ]
            if not requested:
                status = CoverageStatus.PENDING
                gaps = ["本轮未请求"]
                matched_ids = []
            elif extraction_gaps:
                status = _missing_status(request)
                gaps = list(dict.fromkeys(extraction_gaps))
                module_evidence_ids.extend(matched_ids)
                module_gaps.extend(gaps)
            elif matched_ids:
                status = CoverageStatus.READY
                gaps = []
                module_evidence_ids.extend(matched_ids)
            else:
                status = _missing_status(request)
                gaps = [f"{submodule_id} 缺少可追溯客户证据"]
                module_gaps.extend(gaps)
            submodules[submodule_id] = SubmoduleCoverageEntry(
                submodule_id=submodule_id,
                status=status,
                evidence_ids=matched_ids,
                gaps=gaps,
            )

        if not requested:
            module_status = CoverageStatus.PENDING
            module_gaps = ["本轮未请求"]
        elif global_extraction_gaps or module_extraction_gaps:
            module_status = _missing_status(request)
            module_gaps.extend(global_extraction_gaps)
            module_gaps.extend(module_extraction_gaps)
        elif any(entry.status is CoverageStatus.BLOCKED for entry in submodules.values()):
            module_status = CoverageStatus.BLOCKED
        elif all(entry.status is CoverageStatus.READY for entry in submodules.values()):
            module_status = CoverageStatus.READY
        else:
            module_status = CoverageStatus.PENDING

        entries[module_id] = CoverageEntry(
            module_id=module_id,
            status=module_status,
            evidence_ids=list(dict.fromkeys(module_evidence_ids)),
            gaps=list(dict.fromkeys(module_gaps)),
            submodules=submodules,
        )
    return CoverageMatrix(entries=entries)

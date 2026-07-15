"""Deterministic evidence coverage evaluation for the fixed report taxonomy."""

from .models import (
    REPORT_MODULE_IDS,
    CoverageEntry,
    CoverageMatrix,
    CoverageStatus,
    EvidenceItem,
    ReportRequest,
    SubmoduleCoverageEntry,
)
from .taxonomy import REPORT_TAXONOMY


def _missing_status(request: ReportRequest) -> CoverageStatus:
    if request.missing_evidence_policy == "block":
        return CoverageStatus.BLOCKED
    return CoverageStatus.PENDING


def evaluate_coverage(
    request: ReportRequest,
    evidence_items: list[EvidenceItem],
) -> CoverageMatrix:
    """Evaluate each requested submodule using only explicitly classified evidence."""

    entries: dict[str, CoverageEntry] = {}
    for module_id in REPORT_MODULE_IDS:
        definition = REPORT_TAXONOMY[module_id]
        requested = module_id in request.target_modules
        submodules: dict[str, SubmoduleCoverageEntry] = {}
        module_evidence_ids: list[str] = []
        module_gaps: list[str] = []
        for submodule_id in definition.submodules:
            matched_ids = [
                item.id
                for item in evidence_items
                if item.module_id == module_id and item.submodule_id == submodule_id
            ]
            if not requested:
                status = CoverageStatus.PENDING
                gaps = ["本轮未请求"]
                matched_ids = []
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
            gaps=module_gaps,
            submodules=submodules,
        )
    return CoverageMatrix(entries=entries)

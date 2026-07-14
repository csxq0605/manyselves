from __future__ import annotations

from dataclasses import dataclass

from pds_report.domain.models import (
    CoverageMatrix,
    EvidenceItem,
    ModuleDraft,
    ModuleTask,
    OutputArtifact,
    ParsedArtifact,
    ProjectManifest,
    ReportRequest,
    ReviewIssue,
)


@dataclass(frozen=True, slots=True)
class CarrierContract:
    model: type[object]
    many: bool = False

    def accepts(self, value: object) -> bool:
        if self.many:
            return isinstance(value, list) and all(
                isinstance(item, self.model) for item in value
            )
        return isinstance(value, self.model)


CARRIER_CONTRACTS: dict[str, CarrierContract] = {
    "report_request": CarrierContract(ReportRequest),
    "project_manifest": CarrierContract(ProjectManifest),
    "parsed_artifacts": CarrierContract(ParsedArtifact, many=True),
    "evidence_items": CarrierContract(EvidenceItem, many=True),
    "coverage_matrix": CarrierContract(CoverageMatrix),
    "module_tasks": CarrierContract(ModuleTask, many=True),
    "module_drafts": CarrierContract(ModuleDraft, many=True),
    "review_issues": CarrierContract(ReviewIssue, many=True),
    "output_artifacts": CarrierContract(OutputArtifact, many=True),
    "run_summary": CarrierContract(dict),
}

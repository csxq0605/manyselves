"""Capability-owned projection of the audited report into render inputs."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from manyselves.capabilities.distribution_reporting.domain.claim_ledger import (
    ClaimLedger,
)

from .assets import ReportAssetAssembler, expand_approved_module_markers
from .models.agentic import EditedReportSubmission, ModuleSubmission
from .rendering.pds_docx_renderer import ApprovedReport, PdsDocxRenderer
from .source_ledger import SourceLedger


def build_delivery_projection(
    workspace: Path,
    state: dict[str, Any],
    edited: EditedReportSubmission,
    claims: list[Any] | None = None,
) -> tuple[ApprovedReport, str]:
    """Build the citation-bound Markdown passed to the Capability renderer."""

    source_modules = dict(state.get("markdown_modules", {}))
    source_modules.update({
        module_id: ModuleSubmission.model_validate(module).markdown
        for module_id, module in state.get("module_submissions", {}).items()
    })
    edited = expand_approved_module_markers(edited, source_modules)
    if claims is None:
        claims = [
            claim
            for module in state.get("module_submissions", {}).values()
            for claim in module.claims
        ]
    ledger = ClaimLedger(
        claims=claims,
        sources=SourceLedger(workspace, state["run_id"]).records,
    )
    tables, photos = ReportAssetAssembler(Path(workspace)).build(
        state.get("evidence_items", []),
        state.get("photo_assets", []),
        claims,
        edited,
    )
    report = ApprovedReport(
        title=edited.title,
        assessment_background=edited.assessment_background,
        findings_overview=edited.findings_overview,
        regional_executive_summary=edited.regional_executive_summary,
        module_narratives=dict(edited.module_narratives),
        risk_panorama=edited.risk_panorama,
        dimension_risk_analysis=edited.dimension_risk_analysis,
        data_gap_analysis=edited.data_gap_analysis,
        improvement_action_plan=edited.improvement_action_plan,
        special_topic_plan=edited.special_topic_plan,
        special_topic_analysis=edited.special_topic_analysis,
        ledger=ledger,
        tables=tables,
        photos=photos,
    )
    return report, ledger.bind_citations(PdsDocxRenderer._compose_markdown(report))


__all__ = ["build_delivery_projection"]

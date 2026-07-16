"""Deterministic validation and assembly of editor-selected report assets."""

from __future__ import annotations

from pathlib import Path

from .agentic_models import ClaimRecord, EditedReportSubmission
from .models import EvidenceItem, PhotoAsset
from .rendering.pds_docx_renderer import ReportPhoto, ReportTable


def validate_editor_protection(edited: EditedReportSubmission, claims: list[ClaimRecord]) -> None:
    """Require exact Claim protection and unique citation anchors in owned modules."""

    claim_by_id = {claim.id: claim for claim in claims}
    if len(claim_by_id) != len(claims):
        raise ValueError("approved claims contain duplicate ids")
    protected = set(edited.protected_claim_ids)
    expected = set(claim_by_id)
    if protected != expected:
        missing = sorted(expected - protected)
        extra = sorted(protected - expected)
        raise ValueError(f"protected claim set mismatch: missing={missing}, extra={extra}")

    unknown_anchors = sorted(set(edited.citation_anchors) - expected)
    if unknown_anchors:
        raise ValueError(f"citation anchors reference unknown claims: {unknown_anchors}")
    for claim in claims:
        if not claim.footnote_required:
            continue
        anchor = edited.citation_anchors.get(claim.id, "")
        narrative = edited.module_narratives[claim.module_id]
        if not anchor or narrative.count(anchor) != 1:
            raise ValueError(
                f"citation anchor for {claim.id} must occur exactly once in module "
                f"{claim.module_id}"
            )


class ReportAssetAssembler:
    """Resolve typed editor selections into renderer assets with project traceability."""

    def __init__(self, workspace: Path):
        self.workspace = Path(workspace).resolve()

    def build(
        self,
        evidence: list[EvidenceItem],
        photos: list[PhotoAsset],
        claims: list[ClaimRecord],
        edited: EditedReportSubmission,
    ) -> tuple[list[ReportTable], list[ReportPhoto]]:
        evidence_by_id = {item.id: item for item in evidence}
        photo_by_id = {photo.id: photo for photo in photos}
        claim_by_id = {claim.id: claim for claim in claims}

        tables = [
            ReportTable(
                title=table.title,
                headers=table.headers,
                rows=table.rows,
                source_ids=table.source_ids,
                claim_ids=table.claim_ids,
            )
            for table in edited.tables
        ]

        report_photos: list[ReportPhoto] = []
        for photo_id in edited.photo_ids:
            asset = photo_by_id.get(photo_id)
            if asset is None:
                raise ValueError(f"editor selected unknown photo {photo_id}")
            path = (self.workspace / asset.path).resolve()
            if not path.is_relative_to(self.workspace) or not path.is_file():
                raise ValueError(f"photo {photo_id} is not a project-local file")
            candidates = sorted(
                (item for item in evidence if photo_id in item.photo_refs),
                key=lambda item: item.id,
            )
            bindings: list[tuple[EvidenceItem, list[str]]] = []
            for item in candidates:
                linked_claim_ids = sorted(
                    claim.id for claim in claims if item.id in claim.source_ids
                )
                if linked_claim_ids:
                    bindings.append((item, linked_claim_ids))
            if not bindings:
                raise ValueError(
                    f"photo {photo_id} requires evidence and at least one linked claim"
                )
            source, claim_ids = bindings[0]
            if source.id not in evidence_by_id or any(
                claim_id not in claim_by_id for claim_id in claim_ids
            ):
                raise AssertionError("asset binding indexes are inconsistent")
            report_photos.append(
                ReportPhoto(
                    id=photo_id,
                    path=path,
                    caption=f"{source.subject}：{source.fact}",
                    source_id=source.id,
                    claim_ids=claim_ids,
                )
            )
        return tables, report_photos

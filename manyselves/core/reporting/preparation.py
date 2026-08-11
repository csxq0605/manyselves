"""Deterministic per-file reporting preparation workers.

Workers parse only one immutable manifest entry and never allocate run-global
E-* or P-* identifiers.  The coordinator reduces results in manifest order.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field

from .intake.adapters import IntakeAdapterRegistry
from .intake.wps_images import extract_wps_images
from .mappers import map_s2_1, map_s4_4, map_s4_6
from .models import (
    EvidenceItem,
    ManifestFile,
    ParsedArtifact,
    PhotoAsset,
    ReportingModel,
)


class FilePreparationResult(ReportingModel):
    """Complete provisional result for one manifest file."""

    schema_version: Literal["1"] = "1"
    manifest_order: int = Field(ge=0)
    file_id: str = Field(min_length=1)
    source_path: Path
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    purpose: str | None = None
    status: Literal["parsed", "failed"]
    parsed_artifacts: list[ParsedArtifact] = Field(default_factory=list)
    provisional_evidence: list[EvidenceItem] = Field(default_factory=list)
    raw_photo_assets: dict[str, PhotoAsset] = Field(default_factory=dict)
    mapping_gaps: list[dict] = Field(default_factory=list)
    error: str | None = None


_MAPPERS = {
    "s2-1": map_s2_1,
    "s4-4": map_s4_4,
    "s4-6": map_s4_6,
}


def prepare_manifest_file(
    workspace: Path,
    run_id: str,
    manifest_file: ManifestFile,
    manifest_order: int,
) -> FilePreparationResult:
    """Parse one file without mutating shared run registries or final assets."""

    workspace = Path(workspace).resolve()
    input_path = workspace / (manifest_file.snapshot_ref or manifest_file.path)
    mapper = _MAPPERS.get(manifest_file.purpose or "")
    try:
        if mapper is None:
            parsed = IntakeAdapterRegistry().parse(input_path, manifest_file)
            return FilePreparationResult(
                manifest_order=manifest_order,
                file_id=manifest_file.id,
                source_path=manifest_file.path,
                source_sha256=manifest_file.sha256,
                purpose=manifest_file.purpose,
                status="parsed",
                parsed_artifacts=parsed,
            )

        raw_photo_assets: dict[str, PhotoAsset] = {}
        if manifest_file.purpose == "s4-4":
            raw_photo_assets = extract_wps_images(
                input_path,
                output_dir=(
                    workspace
                    / "Work"
                    / "runs"
                    / run_id
                    / "preparation-workers"
                    / f"{manifest_order:04d}-{manifest_file.id}"
                    / "assets"
                ),
            )
        mapped = mapper(input_path, file_id=manifest_file.id)
        evidence = [
            item.model_copy(
                update={
                    "source": item.source.model_copy(
                        update={"path": manifest_file.path}
                    )
                }
            )
            for item in mapped.evidence_items
        ]
        return FilePreparationResult(
            manifest_order=manifest_order,
            file_id=manifest_file.id,
            source_path=manifest_file.path,
            source_sha256=manifest_file.sha256,
            purpose=manifest_file.purpose,
            status="parsed",
            provisional_evidence=evidence,
            raw_photo_assets=raw_photo_assets,
            mapping_gaps=[gap.model_dump(mode="json") for gap in mapped.gaps],
        )
    except Exception as exc:
        return FilePreparationResult(
            manifest_order=manifest_order,
            file_id=manifest_file.id,
            source_path=manifest_file.path,
            source_sha256=manifest_file.sha256,
            purpose=manifest_file.purpose,
            status="failed",
            error=str(exc),
        )

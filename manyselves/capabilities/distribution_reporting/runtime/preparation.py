"""Deterministic per-file reporting preparation workers.

Workers parse only one immutable manifest entry and never allocate run-global
E-* or P-* identifiers.  The coordinator reduces results in manifest order.
"""

from __future__ import annotations

from pathlib import Path

from manyselves.capabilities.distribution_reporting.runtime.models import (
    preparation as preparation_models,
)
from manyselves.capabilities.distribution_reporting.runtime.models.reporting import (
    PhotoAsset,
)

from .intake.adapters import IntakeAdapterRegistry
from .intake.wps_images import extract_wps_images
from .mappers import map_s2_1, map_s4_4, map_s4_6

_MAPPERS = {
    "s2-1": map_s2_1,
    "s4-4": map_s4_4,
    "s4-6": map_s4_6,
}


def prepare_manifest_file(
    workspace: Path,
    run_id: str,
    manifest_file: preparation_models.ManifestFile,
    manifest_order: int,
) -> preparation_models.FilePreparationResult:
    """Parse one file without mutating shared run registries or final assets."""

    workspace = Path(workspace).resolve()
    input_path = workspace / (manifest_file.snapshot_ref or manifest_file.path)
    mapper = _MAPPERS.get(manifest_file.purpose or "")
    try:
        if mapper is None:
            parsed = IntakeAdapterRegistry().parse(input_path, manifest_file)
            return preparation_models.FilePreparationResult(
                manifest_order=manifest_order,
                file_id=manifest_file.id,
                source_path=manifest_file.path,
                source_sha256=manifest_file.sha256,
                purpose=manifest_file.purpose,
                status="parsed",
                parsed_artifacts=parsed,
            )

        mapped = mapper(input_path, file_id=manifest_file.id)
        referenced_photo_ids = {
            photo_id
            for item in mapped.evidence_items
            for photo_id in item.photo_refs
        }
        raw_photo_assets: dict[str, PhotoAsset] = {}
        if referenced_photo_ids:
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
                required_image_ids=referenced_photo_ids,
            )
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
        return preparation_models.FilePreparationResult(
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
        return preparation_models.FilePreparationResult(
            manifest_order=manifest_order,
            file_id=manifest_file.id,
            source_path=manifest_file.path,
            source_sha256=manifest_file.sha256,
            purpose=manifest_file.purpose,
            status="failed",
            error=str(exc),
        )

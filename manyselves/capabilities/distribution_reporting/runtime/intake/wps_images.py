"""Extract WPS ``DISPIMG`` cell images through their OOXML relationships."""

import hashlib
import mimetypes
from pathlib import Path, PurePosixPath
from xml.etree import ElementTree
from zipfile import BadZipFile, ZipFile

from manyselves.capabilities.distribution_reporting.runtime.models.reporting import (
    EvidenceItem,
    PhotoAsset,
)

_CELL_IMAGES = "xl/cellimages.xml"
_CELL_IMAGE_RELS = "xl/_rels/cellimages.xml.rels"
_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_DRAWING_NS = "http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing"
_A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
_OFFICE_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def _media_member(target: str) -> str:
    member = PurePosixPath("xl") / PurePosixPath(target)
    if ".." in member.parts:
        raise ValueError(f"unsafe WPS image relationship target: {target}")
    return member.as_posix()


def extract_wps_images(
    workbook_path: Path,
    *,
    output_dir: Path,
    required_image_ids: set[str] | None = None,
) -> dict[str, PhotoAsset]:
    """Extract referenced images keyed by the identifier used by ``DISPIMG``.

    ``cellimages.xml`` may also contain decorative/template images.  Callers
    that already mapped workbook evidence should pass its exact photo refs so
    only report evidence assets are materialized.
    """

    workbook_path = Path(workbook_path)
    output_dir = Path(output_dir)
    try:
        archive = ZipFile(workbook_path)
    except BadZipFile:
        return {}

    with archive:
        names = set(archive.namelist())
        if _CELL_IMAGES not in names or _CELL_IMAGE_RELS not in names:
            return {}

        relationships_root = ElementTree.fromstring(archive.read(_CELL_IMAGE_RELS))
        targets = {
            relationship.attrib["Id"]: _media_member(relationship.attrib["Target"])
            for relationship in relationships_root.findall(f"{{{_REL_NS}}}Relationship")
            if "Id" in relationship.attrib and "Target" in relationship.attrib
        }
        images_root = ElementTree.fromstring(archive.read(_CELL_IMAGES))
        output_dir.mkdir(parents=True, exist_ok=True)
        assets: dict[str, PhotoAsset] = {}
        pictures = images_root.findall(f".//{{{_DRAWING_NS}}}pic")
        for picture_index, picture in enumerate(pictures, start=1):
            properties = picture.find(f".//{{{_DRAWING_NS}}}cNvPr")
            blip = picture.find(f".//{{{_A_NS}}}blip")
            if properties is None or blip is None:
                continue
            image_id = properties.attrib.get("name", "").strip()
            if required_image_ids is not None and image_id not in required_image_ids:
                continue
            relationship_id = blip.attrib.get(f"{{{_OFFICE_REL_NS}}}embed", "")
            member = targets.get(relationship_id)
            if not image_id or not member or member not in names:
                continue
            if image_id in assets:
                raise ValueError(f"duplicate WPS image identifier: {image_id}")
            content = archive.read(member)
            suffix = PurePosixPath(member).suffix.casefold() or ".bin"
            destination = output_dir / f"source-{picture_index:04d}{suffix}"
            destination.write_bytes(content)
            media_type = mimetypes.guess_type(destination.name)[0] or "application/octet-stream"
            assets[image_id] = PhotoAsset(
                id=image_id,
                path=destination,
                sha256=hashlib.sha256(content).hexdigest(),
                media_type=media_type,
                source_member=member,
                source_image_id=image_id,
            )
        return assets


def canonicalize_photo_bindings(
    evidence: list[EvidenceItem],
    assets: dict[str, PhotoAsset],
    *,
    start_index: int,
) -> tuple[list[EvidenceItem], list[PhotoAsset]]:
    """Replace workbook-private image keys with ordered run-facing photo IDs."""

    raw_to_canonical = {
        raw_id: f"P-{start_index + offset:04d}"
        for offset, raw_id in enumerate(assets)
    }
    referenced = {
        photo_id
        for item in evidence
        for photo_id in item.photo_refs
    }
    unknown = sorted(referenced - set(raw_to_canonical))
    if unknown:
        raise ValueError(f"evidence references unextractable workbook photos: {unknown}")
    evidence_by_photo: dict[str, list[str]] = {}
    for item in evidence:
        for photo_id in item.photo_refs:
            evidence_by_photo.setdefault(photo_id, []).append(item.id)
    unbound = [
        raw_id for raw_id in raw_to_canonical if not evidence_by_photo.get(raw_id)
    ]
    if unbound:
        raise ValueError(
            "each extracted photo requires source-table evidence and a smallest "
            f"submodule: {unbound}"
        )
    normalized_evidence = [
        item.model_copy(
            update={
                "photo_refs": [
                    raw_to_canonical[photo_id] for photo_id in item.photo_refs
                ]
            }
        )
        for item in evidence
    ]
    planned_assets: list[tuple[str, PhotoAsset, str, Path]] = []
    for raw_id, asset in assets.items():
        canonical_id = raw_to_canonical[raw_id]
        canonical_path = asset.path.with_name(
            f"{canonical_id}{asset.path.suffix.casefold()}"
        )
        if canonical_path != asset.path and canonical_path.exists():
            raise FileExistsError(f"canonical photo path already exists: {canonical_path}")
        planned_assets.append((raw_id, asset, canonical_id, canonical_path))

    normalized_assets: list[PhotoAsset] = []
    for raw_id, asset, canonical_id, canonical_path in planned_assets:
        if canonical_path != asset.path:
            asset.path.replace(canonical_path)
        normalized_assets.append(
            asset.model_copy(
                update={
                    "id": canonical_id,
                    "path": canonical_path,
                    "source_image_id": raw_id,
                    # Mapper output follows source-table row/column order. Keep
                    # every association, but make the first source occurrence
                    # the explicit and persisted caption/placement owner.
                    "primary_evidence_id": evidence_by_photo[raw_id][0],
                }
            )
        )
    return normalized_evidence, normalized_assets

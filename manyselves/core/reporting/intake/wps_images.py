"""Extract WPS ``DISPIMG`` cell images through their OOXML relationships."""

import hashlib
import mimetypes
from pathlib import Path, PurePosixPath
from xml.etree import ElementTree
from zipfile import BadZipFile, ZipFile

from ..models import PhotoAsset

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
) -> dict[str, PhotoAsset]:
    """Extract images keyed by the identifier used by ``DISPIMG`` formulas."""

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
        for picture in pictures:
            properties = picture.find(f".//{{{_DRAWING_NS}}}cNvPr")
            blip = picture.find(f".//{{{_A_NS}}}blip")
            if properties is None or blip is None:
                continue
            image_id = properties.attrib.get("name", "").strip()
            relationship_id = blip.attrib.get(f"{{{_OFFICE_REL_NS}}}embed", "")
            member = targets.get(relationship_id)
            if not image_id or not member or member not in names:
                continue
            content = archive.read(member)
            suffix = PurePosixPath(member).suffix.casefold() or ".bin"
            destination = output_dir / f"{image_id}{suffix}"
            destination.write_bytes(content)
            media_type = mimetypes.guess_type(destination.name)[0] or "application/octet-stream"
            assets[image_id] = PhotoAsset(
                id=image_id,
                path=destination,
                sha256=hashlib.sha256(content).hexdigest(),
                media_type=media_type,
                source_member=member,
            )
        return assets

"""Bounded passive preview normalization for supported workspace documents."""

import base64
import csv
import html
import io
import mimetypes
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from openpyxl import load_workbook
from PIL import Image

from .workspace_files import WorkspaceFileError, WorkspaceFiles


class PreviewError(WorkspaceFileError):
    """Base class for stable preview failures."""


class PreviewTooLarge(PreviewError):  # noqa: N818 - stable application error
    code = "PREVIEW_TOO_LARGE"

    def __init__(self) -> None:
        super().__init__("File exceeds the configured preview limit")


class PreviewEncodingError(PreviewError):
    code = "PREVIEW_ENCODING_ERROR"

    def __init__(self) -> None:
        super().__init__("Preview text is not valid UTF-8")


class UnsupportedPreview(PreviewError):  # noqa: N818 - stable application error
    code = "PREVIEW_UNSUPPORTED"

    def __init__(self) -> None:
        super().__init__("Preview format is unsupported")


class InvalidPreviewDocument(PreviewError):  # noqa: N818 - stable application error
    code = "PREVIEW_INVALID_DOCUMENT"

    def __init__(self) -> None:
        super().__init__("Preview document is invalid")


@dataclass(frozen=True, slots=True)
class PreviewCapture:
    """Bounded immutable bytes captured while the facade read lock is held."""

    path: str
    name: str
    suffix: str
    raw: bytes


class PreviewService:
    """Return bounded JSON-compatible DTOs without evaluating active content."""

    def __init__(
        self,
        files: WorkspaceFiles,
        *,
        max_preview_bytes: int = 8 * 1024 * 1024,
        max_archive_expanded_bytes: int = 32 * 1024 * 1024,
        row_limit: int = 200,
        column_limit: int = 100,
        block_limit: int = 500,
        archive_member_limit: int = 2_000,
        sheet_limit: int = 100,
        cell_character_limit: int = 4_096,
    ) -> None:
        if min(
            max_preview_bytes,
            max_archive_expanded_bytes,
            row_limit,
            column_limit,
            block_limit,
            archive_member_limit,
            sheet_limit,
            cell_character_limit,
        ) <= 0:
            raise ValueError("Preview limits must be positive")
        self._files = files
        self._max_preview_bytes = max_preview_bytes
        self._max_archive_expanded_bytes = max_archive_expanded_bytes
        self._row_limit = row_limit
        self._column_limit = column_limit
        self._block_limit = block_limit
        self._archive_member_limit = archive_member_limit
        self._sheet_limit = sheet_limit
        self._cell_character_limit = cell_character_limit

    def preview(self, relative_path: str, *, content_url: str) -> dict[str, Any]:
        """Convenience synchronous capture and parse for non-HTTP callers."""
        return self.preview_capture(self.capture(relative_path), content_url=content_url)

    def capture(self, relative_path: str) -> PreviewCapture:
        """Copy bounded file bytes for parsing after the filesystem transaction releases."""
        path = self._files.file_path(relative_path)
        return PreviewCapture(
            path=self._files.relative(path),
            name=path.name,
            suffix=path.suffix.casefold(),
            raw=self._read_bounded(path),
        )

    def preview_capture(
        self,
        capture: PreviewCapture,
        *,
        content_url: str,
    ) -> dict[str, Any]:
        """Parse an immutable capture without touching the workspace filesystem."""
        if capture.suffix == ".pdf":
            return self._pdf(capture.raw, capture.path, content_url)
        if capture.suffix == ".svg":
            return self._svg(capture.raw, capture.path)
        if capture.suffix in {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}:
            return self._image(capture.raw, capture.name, capture.path, content_url)
        if capture.suffix in {".xlsx", ".csv"}:
            return self._spreadsheet(capture.raw, capture.name, capture.suffix, capture.path)
        if capture.suffix == ".docx":
            return self._docx(capture.raw, capture.path)
        if capture.suffix == ".md":
            text = self._decode_utf8(capture.raw)
            return {
                "type": "markdown",
                "path": capture.path,
                "content": html.escape(text, quote=False),
                "truncated": False,
            }
        if capture.suffix in {".py", ".json", ".yaml", ".yml", ".txt", ".log"}:
            return {
                "type": "text",
                "path": capture.path,
                "content": self._decode_utf8(capture.raw),
                "truncated": False,
            }
        return {
            "type": "unsupported",
            "path": capture.path,
            "mimeType": mimetypes.guess_type(capture.name)[0] or "application/octet-stream",
            "size": len(capture.raw),
            "downloadUrl": content_url,
        }

    def _pdf(self, raw: bytes, portable_path: str, content_url: str) -> dict[str, Any]:
        if not raw.startswith(b"%PDF-"):
            raise InvalidPreviewDocument()
        return {
            "type": "pdf",
            "path": portable_path,
            "mimeType": "application/pdf",
            "size": len(raw),
            "rangeUrl": content_url,
        }

    def _image(
        self,
        raw: bytes,
        name: str,
        portable_path: str,
        content_url: str,
    ) -> dict[str, Any]:
        try:
            with Image.open(io.BytesIO(raw)) as image:
                width, height = image.size
                image_format = image.format
                image.verify()
        except Exception as error:
            raise InvalidPreviewDocument() from error
        if width <= 0 or height <= 0 or width * height > 40_000_000:
            raise PreviewTooLarge()
        mime_type = Image.MIME.get(image_format or "") or mimetypes.guess_type(name)[0]
        return {
            "type": "image",
            "path": portable_path,
            "mimeType": mime_type or "application/octet-stream",
            "width": width,
            "height": height,
            "contentUrl": content_url,
        }

    def _svg(self, raw: bytes, portable_path: str) -> dict[str, Any]:
        try:
            root = ElementTree.fromstring(raw)
        except (ElementTree.ParseError, ValueError) as error:
            raise InvalidPreviewDocument() from error
        if _local_name(root.tag).casefold() != "svg" or _namespace(root.tag) not in {
            "",
            "http://www.w3.org/2000/svg",
        }:
            raise InvalidPreviewDocument()
        sanitized = _sanitize_svg_node(root)
        if sanitized is None:
            raise InvalidPreviewDocument()
        content = ElementTree.tostring(sanitized, encoding="unicode")
        return {
            "type": "image",
            "path": portable_path,
            "mimeType": "image/svg+xml",
            "content": content,
        }

    def _spreadsheet(
        self,
        raw: bytes,
        name: str,
        suffix: str,
        portable_path: str,
    ) -> dict[str, Any]:
        if suffix == ".csv":
            text = self._decode_utf8(raw)
            row_count = 0
            column_count = 0
            rows: list[list[str]] = []
            cells_truncated = False
            try:
                for row in csv.reader(io.StringIO(text)):
                    row_count += 1
                    column_count = max(column_count, len(row))
                    if len(rows) < self._row_limit:
                        serialized = []
                        for value in row[: self._column_limit]:
                            cell, truncated = _cell_text(value, self._cell_character_limit)
                            serialized.append(cell)
                            cells_truncated = cells_truncated or truncated
                        rows.append(serialized)
            except csv.Error as error:
                raise InvalidPreviewDocument() from error
            return {
                "type": "spreadsheet",
                "path": portable_path,
                "sheets": [
                    {
                        "name": Path(name).stem,
                        "rowCount": row_count,
                        "columnCount": column_count,
                        "rows": rows,
                        "truncated": row_count > self._row_limit
                        or column_count > self._column_limit
                        or cells_truncated,
                    }
                ],
            }

        self._validate_archive(raw)
        try:
            workbook = load_workbook(
                io.BytesIO(raw),
                read_only=True,
                data_only=True,
                keep_links=False,
            )
        except Exception as error:
            raise InvalidPreviewDocument() from error
        try:
            sheets = []
            worksheets = workbook.worksheets
            for sheet in worksheets[: self._sheet_limit]:
                rows = []
                cells_truncated = False
                for row in sheet.iter_rows(
                    max_row=self._row_limit,
                    max_col=self._column_limit,
                ):
                    serialized = []
                    for cell in row[: self._column_limit]:
                        value, truncated = _cell_text(
                            cell.value,
                            self._cell_character_limit,
                        )
                        serialized.append(value)
                        cells_truncated = cells_truncated or truncated
                    rows.append(serialized)
                sheets.append(
                    {
                        "name": sheet.title,
                        "rowCount": sheet.max_row,
                        "columnCount": sheet.max_column,
                        "rows": rows,
                        "truncated": sheet.max_row > self._row_limit
                        or sheet.max_column > self._column_limit
                        or cells_truncated,
                    }
                )
            return {
                "type": "spreadsheet",
                "path": portable_path,
                "sheets": sheets,
                "sheetsTruncated": len(worksheets) > self._sheet_limit,
            }
        finally:
            workbook.close()

    def _docx(self, raw: bytes, portable_path: str) -> dict[str, Any]:
        self._validate_archive(raw)
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                document = ElementTree.fromstring(archive.read("word/document.xml"))
                relationships = _docx_relationships(archive)
                blocks: list[dict[str, Any]] = []
                body = next(
                    (node for node in document.iter() if _local_name(node.tag) == "body"), None
                )
                if body is None:
                    raise InvalidPreviewDocument()
                for node in body:
                    if len(blocks) >= self._block_limit:
                        break
                    kind = _local_name(node.tag)
                    if kind == "p":
                        text = "".join(
                            child.text or ""
                            for child in node.iter()
                            if _local_name(child.tag) == "t"
                        )
                        if text:
                            blocks.append({"type": "paragraph", "text": text})
                        for child in node.iter():
                            if len(blocks) >= self._block_limit:
                                break
                            if _local_name(child.tag) != "blip":
                                continue
                            relationship_id = next(
                                (
                                    value
                                    for name, value in child.attrib.items()
                                    if _local_name(name) == "embed"
                                ),
                                None,
                            )
                            target = relationships.get(relationship_id or "")
                            if target is None or not target.startswith("word/media/"):
                                continue
                            image = archive.read(target)
                            mime = mimetypes.guess_type(target)[0] or "application/octet-stream"
                            blocks.append(
                                {
                                    "type": "image",
                                    "mimeType": mime,
                                    "dataUrl": f"data:{mime};base64,{base64.b64encode(image).decode('ascii')}",
                                }
                            )
                    elif kind == "tbl":
                        rows = []
                        for row in (child for child in node if _local_name(child.tag) == "tr"):
                            cells = []
                            for cell in (child for child in row if _local_name(child.tag) == "tc"):
                                cells.append(
                                    _bounded_text(
                                        "".join(
                                            value.text or ""
                                            for value in cell.iter()
                                            if _local_name(value.tag) == "t"
                                        ),
                                        self._cell_character_limit,
                                    )
                                )
                            rows.append(cells[: self._column_limit])
                            if len(rows) >= self._row_limit:
                                break
                        blocks.append({"type": "table", "rows": rows})
                return {
                    "type": "docx",
                    "path": portable_path,
                    "blocks": blocks,
                    "truncated": len(body) > len(blocks),
                }
        except (KeyError, OSError, zipfile.BadZipFile, ElementTree.ParseError) as error:
            raise InvalidPreviewDocument() from error

    def _read_bounded(self, path: Path) -> bytes:
        if path.stat().st_size > self._max_preview_bytes:
            raise PreviewTooLarge()
        with path.open("rb") as stream:
            raw = stream.read(self._max_preview_bytes + 1)
        if len(raw) > self._max_preview_bytes:
            raise PreviewTooLarge()
        return raw

    def _validate_archive(self, raw: bytes) -> None:
        try:
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                infos = archive.infolist()
                if len(infos) > self._archive_member_limit:
                    raise PreviewTooLarge()
                if any(info.flag_bits & 0x1 for info in infos):
                    raise InvalidPreviewDocument()
                if sum(info.file_size for info in infos) > self._max_archive_expanded_bytes:
                    raise PreviewTooLarge()
        except zipfile.BadZipFile as error:
            raise InvalidPreviewDocument() from error

    @staticmethod
    def _decode_utf8(raw: bytes) -> str:
        try:
            return raw.decode("utf-8")
        except UnicodeDecodeError as error:
            raise PreviewEncodingError() from error


def _local_name(name: str) -> str:
    return name.rsplit("}", 1)[-1]


def _namespace(name: str) -> str:
    return name[1:].split("}", 1)[0] if name.startswith("{") else ""


_SVG_NAMESPACE = "http://www.w3.org/2000/svg"
_SVG_ELEMENTS = {
    "circle",
    "clippath",
    "defs",
    "desc",
    "ellipse",
    "g",
    "lineargradient",
    "line",
    "mask",
    "path",
    "pattern",
    "polygon",
    "polyline",
    "radialgradient",
    "rect",
    "stop",
    "svg",
    "text",
    "title",
    "tspan",
    "use",
}
_SVG_ATTRIBUTES = {
    "class",
    "clip-path",
    "cx",
    "cy",
    "d",
    "dominant-baseline",
    "fill",
    "fill-opacity",
    "font-family",
    "font-size",
    "height",
    "href",
    "id",
    "mask",
    "offset",
    "opacity",
    "patternunits",
    "points",
    "preserveaspectratio",
    "r",
    "rx",
    "ry",
    "stop-color",
    "stop-opacity",
    "stroke",
    "stroke-opacity",
    "stroke-width",
    "text-anchor",
    "transform",
    "viewbox",
    "width",
    "x",
    "x1",
    "x2",
    "y",
    "y1",
    "y2",
}
_SVG_TEXT_ELEMENTS = {"desc", "text", "title", "tspan"}
_SAFE_SVG_FRAGMENT = re.compile(r"#[A-Za-z_][A-Za-z0-9_.-]*\Z")
_SAFE_SVG_PAINT_URL = re.compile(
    r"url\(\s*#[A-Za-z_][A-Za-z0-9_.-]*\s*\)\Z",
    re.IGNORECASE,
)
_EXTERNAL_SVG_MARKERS = ("javascript:", "data:", "http:", "https:", "file:", "//")


def _sanitize_svg_node(node: ElementTree.Element) -> ElementTree.Element | None:
    """Copy only passive SVG elements and attributes into a fresh tree."""
    namespace = _namespace(node.tag)
    element_name = _local_name(node.tag).casefold()
    if namespace not in {"", _SVG_NAMESPACE} or element_name not in _SVG_ELEMENTS:
        return None

    sanitized = ElementTree.Element(node.tag)
    for raw_name, raw_value in node.attrib.items():
        attribute_name = _local_name(raw_name).casefold()
        value = raw_value.strip()
        lowered = value.casefold()
        if attribute_name not in _SVG_ATTRIBUTES or attribute_name.startswith("on"):
            continue
        if any(marker in lowered for marker in _EXTERNAL_SVG_MARKERS):
            continue
        if attribute_name == "href" and _SAFE_SVG_FRAGMENT.fullmatch(value) is None:
            continue
        if "url(" in lowered and _SAFE_SVG_PAINT_URL.fullmatch(value) is None:
            continue
        sanitized.set(raw_name, value)

    if element_name in _SVG_TEXT_ELEMENTS:
        sanitized.text = node.text
    for child in node:
        safe_child = _sanitize_svg_node(child)
        if safe_child is None:
            continue
        if element_name in _SVG_TEXT_ELEMENTS:
            safe_child.tail = child.tail
        sanitized.append(safe_child)
    return sanitized


def _cell_text(value: Any, limit: int) -> tuple[str, bool]:
    text = "" if value is None else str(value)
    return _bounded_text(text, limit), len(text) > limit


def _bounded_text(value: str, limit: int) -> str:
    return value[:limit]


def _docx_relationships(archive: zipfile.ZipFile) -> dict[str, str]:
    try:
        root = ElementTree.fromstring(archive.read("word/_rels/document.xml.rels"))
    except KeyError:
        return {}
    relationships: dict[str, str] = {}
    for node in root:
        relationship_id = node.attrib.get("Id")
        target = node.attrib.get("Target", "")
        target_mode = node.attrib.get("TargetMode", "Internal")
        if relationship_id and target_mode == "Internal" and not target.startswith(("/", "..")):
            relationships[relationship_id] = f"word/{target}"
    return relationships

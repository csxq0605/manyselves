"""Suffix-dispatched intake adapters with explicit manual-review outcomes."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from docx import Document
from openpyxl import load_workbook
from PIL import Image

from manyselves.capabilities.distribution_reporting.runtime.models.preparation import (
    ManifestFile,
    ParsedArtifact,
)
from manyselves.capabilities.distribution_reporting.runtime.models.reporting import (
    SourceLocation,
)

Parser = Callable[[Path, ManifestFile], list[ParsedArtifact]]


def _artifact(
    manifest: ManifestFile,
    kind: str,
    index: int,
    payload: dict,
    **location,
) -> ParsedArtifact:
    return ParsedArtifact(
        id=f"{manifest.id}-{kind}-{index:04d}",
        kind=kind,
        source=SourceLocation(
            file_id=manifest.id,
            path=manifest.path,
            **location,
        ),
        payload=payload,
    )


def _parse_text(path: Path, manifest: ManifestFile) -> list[ParsedArtifact]:
    return [
        _artifact(
            manifest,
            "text",
            1,
            {"text": path.read_text(encoding="utf-8", errors="replace")},
        )
    ]


def _parse_docx(path: Path, manifest: ManifestFile) -> list[ParsedArtifact]:
    document = Document(path)
    return [
        _artifact(manifest, "document_paragraph", index, {"text": paragraph.text}, row=index)
        for index, paragraph in enumerate(document.paragraphs, start=1)
        if paragraph.text.strip()
    ]


def _parse_image(path: Path, manifest: ManifestFile) -> list[ParsedArtifact]:
    with Image.open(path) as image:
        payload = {
            "width": image.width,
            "height": image.height,
            "format": image.format or "unknown",
            "mode": image.mode,
        }
    return [_artifact(manifest, "image_metadata", 1, payload)]


def _parse_workbook(path: Path, manifest: ManifestFile) -> list[ParsedArtifact]:
    workbook = load_workbook(path, read_only=True, data_only=True)
    artifacts: list[ParsedArtifact] = []
    try:
        for sheet in workbook.worksheets:
            rows = sheet.iter_rows(values_only=True)
            first = next(rows, None)
            if first is None:
                continue
            headers = [
                str(value) if value not in (None, "") else f"column_{index + 1}"
                for index, value in enumerate(first)
            ]
            for row_index, row in enumerate(rows, start=2):
                if not any(value not in (None, "") for value in row):
                    continue
                artifacts.append(
                    _artifact(
                        manifest,
                        "workbook_row",
                        len(artifacts) + 1,
                        {"headers": headers, "values": list(row)},
                        sheet=sheet.title,
                        cell=f"A{row_index}",
                        row=row_index,
                    )
                )
    finally:
        workbook.close()
    return artifacts


def _manual(
    manifest: ManifestFile, reason: str, *, detail: str | None = None
) -> list[ParsedArtifact]:
    payload = {"reason": reason}
    if detail:
        payload["detail"] = detail
    return [_artifact(manifest, "manual_required", 1, payload)]


def _parse_pdf(path: Path, manifest: ManifestFile) -> list[ParsedArtifact]:
    try:
        from pypdf import PdfReader
    except ImportError:
        return _manual(manifest, "pdf_parser_unavailable")
    reader = PdfReader(path)
    return [
        _artifact(
            manifest,
            "pdf_page",
            page_number,
            {"text": page.extract_text() or ""},
            page=page_number,
        )
        for page_number, page in enumerate(reader.pages, start=1)
    ]


class IntakeAdapterRegistry:
    """Parse each manifest file without hiding unsupported binary formats."""

    PARSERS: dict[str, Parser] = {
        ".xlsx": _parse_workbook,
        ".xlsm": _parse_workbook,
        ".docx": _parse_docx,
        ".md": _parse_text,
        ".txt": _parse_text,
        ".png": _parse_image,
        ".jpg": _parse_image,
        ".jpeg": _parse_image,
        ".pdf": _parse_pdf,
    }
    MANUAL_REQUIRED = {".dwg", ".mp4", ".mov", ".avi"}

    def parse(self, path: Path, manifest_file: ManifestFile) -> list[ParsedArtifact]:
        path = Path(path)
        suffix = path.suffix.casefold()
        if suffix in self.MANUAL_REQUIRED:
            return _manual(manifest_file, "unsupported_binary_format")
        parser = self.PARSERS.get(suffix)
        if parser is None:
            return _manual(manifest_file, "unsupported_format", detail=suffix or "no_suffix")
        return parser(path, manifest_file)

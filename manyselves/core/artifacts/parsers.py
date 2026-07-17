"""Truthful format-aware artifact extraction."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from docx import Document
from openpyxl import load_workbook
from PIL import Image


@dataclass(frozen=True)
class ArtifactBlock:
    locator: str
    text: str


@dataclass(frozen=True)
class ParsedArtifactBlocks:
    kind: Literal["text", "docx", "xlsx", "pdf", "image_metadata", "unsupported"]
    blocks: tuple[ArtifactBlock, ...]
    visual_verified: bool = False
    error: str | None = None


def parse_artifact(path: Path) -> ParsedArtifactBlocks:
    target = Path(path)
    suffix = target.suffix.casefold()
    if suffix == ".docx":
        document = Document(target)
        blocks = [ArtifactBlock(f"paragraph:{i + 1}", p.text) for i, p in enumerate(document.paragraphs) if p.text]
        for table_index, table in enumerate(document.tables, start=1):
            for row_index, row in enumerate(table.rows, start=1):
                blocks.append(ArtifactBlock(f"table:{table_index}:row:{row_index}", "\t".join(cell.text for cell in row.cells)))
        return ParsedArtifactBlocks("docx", tuple(blocks))
    if suffix in {".xlsx", ".xlsm"}:
        workbook = load_workbook(target, read_only=True, data_only=True)
        blocks = []
        try:
            for sheet in workbook.worksheets:
                for row_index, row in enumerate(sheet.iter_rows(values_only=True), start=1):
                    values = ["" if value is None else str(value) for value in row]
                    if any(values):
                        blocks.append(ArtifactBlock(f"sheet:{sheet.title}:row:{row_index}", "\t".join(values)))
        finally:
            workbook.close()
        return ParsedArtifactBlocks("xlsx", tuple(blocks))
    if suffix == ".pdf":
        try:
            from pypdf import PdfReader

            reader = PdfReader(str(target))
            blocks = tuple(ArtifactBlock(f"page:{i + 1}", page.extract_text() or "") for i, page in enumerate(reader.pages))
            return ParsedArtifactBlocks("pdf", blocks)
        except Exception as exc:
            return ParsedArtifactBlocks("unsupported", (), error=f"PDF extraction unavailable: {exc}")
    if suffix in {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".tiff"}:
        with Image.open(target) as image:
            text = f"format={image.format}; width={image.width}; height={image.height}; mode={image.mode}"
        return ParsedArtifactBlocks("image_metadata", (ArtifactBlock("metadata", text),), visual_verified=False)
    try:
        return ParsedArtifactBlocks("text", (ArtifactBlock("text", target.read_text(encoding="utf-8")),))
    except UnicodeDecodeError as exc:
        return ParsedArtifactBlocks("unsupported", (), error=f"unsupported binary format: {exc}")

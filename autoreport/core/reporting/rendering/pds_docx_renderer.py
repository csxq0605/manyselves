"""Render an approved five-module agent report through the handoff DOCX core."""

from __future__ import annotations

import hashlib
import io
import re
import tempfile
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Cm, Pt
from pydantic import Field, field_validator, model_validator

from ..agentic_models import StrictModel
from ..claim_ledger import ClaimLedger
from ..models import REPORT_MODULE_IDS
from ..taxonomy import REPORT_TAXONOMY
from .handoff_docx import HandoffDocxCore

_CITATION_TOKEN = re.compile(r"\[\[CITE:(\d+)\]\]")
_PHOTO_TOKEN = re.compile(r"^\[\[PHOTO:([^]]+)\]\]$")
_FIXED_DOCX_TIME = datetime(2000, 1, 1, tzinfo=UTC)


class ReportTable(StrictModel):
    title: str = Field(min_length=1)
    headers: list[str] = Field(min_length=1)
    rows: list[list[str]] = Field(default_factory=list)
    source_ids: list[str] = Field(default_factory=list)
    claim_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def rows_match_headers(self) -> "ReportTable":
        if not self.source_ids:
            raise ValueError("table requires at least one source id")
        invalid = [index for index, row in enumerate(self.rows) if len(row) != len(self.headers)]
        if invalid:
            raise ValueError(f"table rows do not match header width: {invalid}")
        return self


class ReportPhoto(StrictModel):
    id: str = Field(min_length=1)
    path: Path
    caption: str = Field(min_length=1)
    source_id: str = Field(pattern=r"^E-")
    claim_ids: list[str] = Field(min_length=1)


class ApprovedReport(StrictModel):
    title: str = Field(min_length=1)
    overview: str = Field(min_length=1)
    module_narratives: dict[str, str]
    conclusion: str = Field(min_length=1)
    ledger: ClaimLedger
    citation_anchors: dict[str, str] = Field(default_factory=dict)
    tables: list[ReportTable] = Field(default_factory=list)
    photos: list[ReportPhoto] = Field(default_factory=list)

    @field_validator("module_narratives")
    @classmethod
    def has_complete_five_modules(cls, narratives: dict[str, str]) -> dict[str, str]:
        if set(narratives) != set(REPORT_MODULE_IDS):
            raise ValueError("approved report requires exactly modules 2.1-2.5")
        if any(not text.strip() for text in narratives.values()):
            raise ValueError("module narratives cannot be blank")
        return narratives

    @model_validator(mode="after")
    def assets_are_traceable(self) -> "ApprovedReport":
        claim_modules = {claim.module_id for claim in self.ledger.claims}
        if claim_modules != set(REPORT_MODULE_IDS):
            raise ValueError("approved report claim ledger requires exactly modules 2.1-2.5")
        source_ids = {source.id for source in self.ledger.sources}
        claim_ids = {claim.id for claim in self.ledger.claims}
        for photo in self.photos:
            if photo.source_id not in source_ids:
                raise ValueError(f"photo {photo.id} references unknown project source")
            unknown_claims = sorted(set(photo.claim_ids) - claim_ids)
            if unknown_claims:
                raise ValueError(f"photo {photo.id} references unknown claims: {unknown_claims}")
        for table in self.tables:
            unknown_sources = sorted(set(table.source_ids) - source_ids)
            if unknown_sources:
                raise ValueError(
                    f"table {table.title} references unknown sources: {unknown_sources}"
                )
            unknown_claims = sorted(set(table.claim_ids) - claim_ids)
            if unknown_claims:
                raise ValueError(f"table {table.title} references unknown claims: {unknown_claims}")
        return self


class PdsRenderResult(StrictModel):
    output_path: Path
    output_sha256: str
    citation_count: int
    table_count: int
    photo_count: int
    handoff_core_used: bool
    protected_prose_verified: bool


class PdsDocxRenderer:
    def __init__(self, handoff_core: HandoffDocxCore):
        self.handoff_core = handoff_core

    def render(self, report: ApprovedReport, output_path: Path) -> PdsRenderResult:
        """Render approved prose only and validate semantic preservation."""

        markdown = self._compose_markdown(report)
        cited_markdown = report.ledger.bind_citations(
            markdown,
            anchors=report.citation_anchors,
        )
        _, raw_docx = self.handoff_core.render_approved_prose(
            cited_markdown,
            filename=Path(output_path).name,
            report_model=None,
        )
        document = Document(io.BytesIO(raw_docx))
        self._ensure_title(document, report.title)
        self._materialize_citations(document)
        self._materialize_photos(document, report)
        self._style_source_index(document)
        document.core_properties.created = _FIXED_DOCX_TIME
        document.core_properties.modified = _FIXED_DOCX_TIME
        document.core_properties.last_printed = _FIXED_DOCX_TIME

        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        buffer = io.BytesIO()
        document.save(buffer)
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                prefix=f".{output_path.stem}-",
                suffix=".docx",
                dir=output_path.parent,
                delete=False,
            ) as temporary:
                temporary.write(self._canonical_docx(buffer.getvalue()))
                temporary_path = Path(temporary.name)
            self._verify_output(temporary_path, report)
            temporary_path.replace(output_path)
            temporary_path = None
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)
        return PdsRenderResult(
            output_path=output_path,
            output_sha256=hashlib.sha256(output_path.read_bytes()).hexdigest(),
            citation_count=len(report.ledger.build_citation_plan().entries),
            table_count=len(report.tables),
            photo_count=len(report.photos),
            handoff_core_used=True,
            protected_prose_verified=True,
        )

    @staticmethod
    def _compose_markdown(report: ApprovedReport) -> str:
        lines = [
            f"# {report.title}",
            "",
            "## 1. 配电评估概述",
            "",
            "### 1.1 评估背景",
            "",
            report.overview,
            "",
            "## 2. 评估内容描述",
            "",
        ]
        claims_by_id = {claim.id: claim for claim in report.ledger.claims}
        for module_id in REPORT_MODULE_IDS:
            module = REPORT_TAXONOMY[module_id]
            lines.extend(
                [
                    f"### {module_id} {module.title}",
                    "",
                    report.module_narratives[module_id],
                    "",
                ]
            )
            for photo in report.photos:
                if any(
                    claims_by_id[claim_id].module_id == module_id for claim_id in photo.claim_ids
                ):
                    lines.extend([f"[[PHOTO:{photo.id}]]", ""])
        for table in report.tables:
            source_note = "、".join(table.source_ids)
            lines.extend(
                [
                    f"{table.title}（来源：{source_note}）",
                    "",
                    PdsDocxRenderer._markdown_table(table),
                    "",
                ]
            )
        lines.extend(
            [
                "## 3. 结论与建议",
                "",
                "### 3.1 风险/问题汇总与概览",
                "",
                report.conclusion,
                "",
                report.ledger.source_index_markdown(),
            ]
        )
        return "\n".join(lines)

    @staticmethod
    def _markdown_table(table: ReportTable) -> str:
        def cell(value: Any) -> str:
            return str(value).replace("|", "\\|").replace("\n", " ")

        lines = [
            "| " + " | ".join(cell(value) for value in table.headers) + " |",
            "| " + " | ".join("---" for _ in table.headers) + " |",
        ]
        lines.extend("| " + " | ".join(cell(value) for value in row) + " |" for row in table.rows)
        return "\n".join(lines)

    @staticmethod
    def _materialize_citations(document: Document) -> None:
        paragraphs = list(document.paragraphs)
        paragraphs.extend(
            p
            for table in document.tables
            for row in table.rows
            for cell in row.cells
            for p in cell.paragraphs
        )
        for paragraph in paragraphs:
            text = paragraph.text
            if not _CITATION_TOKEN.search(text):
                continue
            pieces = _CITATION_TOKEN.split(text)
            paragraph.clear()
            for index, piece in enumerate(pieces):
                if not piece:
                    continue
                run = paragraph.add_run(piece)
                if index % 2 == 1:
                    run.font.superscript = True

    @staticmethod
    def _ensure_title(document: Document, title: str) -> None:
        if any(paragraph.text.strip() == title for paragraph in document.paragraphs):
            return
        paragraph = document.paragraphs[0] if document.paragraphs else document.add_paragraph()
        style_names = {style.name for style in document.styles}
        style = "Title" if "Title" in style_names else "Normal"
        title_paragraph = paragraph.insert_paragraph_before(title, style=style)
        title_paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
        for run in title_paragraph.runs:
            run.bold = True
            run.font.size = Pt(18)

    @staticmethod
    def _materialize_photos(document: Document, report: ApprovedReport) -> None:
        photos = {photo.id: photo for photo in report.photos}
        for paragraph in list(document.paragraphs):
            match = _PHOTO_TOKEN.match(paragraph.text.strip())
            if match is None:
                continue
            photo = photos[match.group(1)]
            if not photo.path.is_file():
                raise FileNotFoundError(f"report photo not found: {photo.path}")
            paragraph.clear()
            paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
            paragraph.add_run().add_picture(str(photo.path), width=Cm(12.0))
            paragraph.add_run(f"\n图 {photo.id}：{photo.caption}（来源 {photo.source_id}）")

    @staticmethod
    def _style_source_index(document: Document) -> None:
        for paragraph in document.paragraphs:
            if paragraph.text == "4. 证据与来源索引":
                paragraph.style = "Heading 1"
            elif paragraph.text in {
                "脚注对应关系",
                "项目证据 E-*",
                "本地参考 R-*",
                "网络来源 W-*",
            }:
                paragraph.style = "Heading 2"

    @staticmethod
    def _canonical_docx(data: bytes) -> bytes:
        source = io.BytesIO(data)
        target = io.BytesIO()
        with (
            zipfile.ZipFile(source, "r") as input_zip,
            zipfile.ZipFile(
                target, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9
            ) as output_zip,
        ):
            for name in sorted(input_zip.namelist()):
                info = zipfile.ZipInfo(name, date_time=(2000, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = input_zip.getinfo(name).external_attr
                output_zip.writestr(info, input_zip.read(name))
        return target.getvalue()

    @staticmethod
    def _verify_output(output_path: Path, report: ApprovedReport) -> None:
        try:
            rendered = Document(output_path)
        except Exception as exc:
            raise ValueError("rendered file is not Word/WPS-openable") from exc
        text = "\n".join(
            "".join(run.text for run in paragraph.runs if run.font.superscript is not True)
            for paragraph in rendered.paragraphs
        )
        protected = [report.overview, report.conclusion, *report.module_narratives.values()]
        missing = [value for value in protected if value not in text]
        if missing:
            raise ValueError("rendered DOCX changed or omitted protected Agent/Chief Editor prose")
        if report.title not in text:
            raise ValueError("rendered DOCX is missing the approved title")
        if _CITATION_TOKEN.search(text):
            raise ValueError("rendered DOCX contains unresolved citation tokens")
        if any(_PHOTO_TOKEN.match(paragraph.text.strip()) for paragraph in rendered.paragraphs):
            raise ValueError("rendered DOCX contains unresolved photo tokens")

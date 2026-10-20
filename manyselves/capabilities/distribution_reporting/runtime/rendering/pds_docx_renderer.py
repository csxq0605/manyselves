"""Render an approved five-module agent report through the handoff DOCX core."""

from __future__ import annotations

import hashlib
import io
import re
import tempfile
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable

from docx import Document
from docx.enum.table import WD_CELL_VERTICAL_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Cm, Pt
from PIL import Image, ImageOps
from pydantic import Field, field_validator, model_validator

from manyselves.capabilities.distribution_reporting.domain.claim_ledger import ClaimLedger
from manyselves.capabilities.distribution_reporting.domain.report_markdown import (
    CanonicalMarkdownTable,
    CanonicalReportContent,
    compose_canonical_markdown,
    markdown_table,
    strip_leading_module_heading,
)
from manyselves.capabilities.distribution_reporting.domain.taxonomy import (
    REPORT_TAXONOMY,
)
from manyselves.capabilities.distribution_reporting.runtime.models.agentic import StrictModel
from manyselves.capabilities.distribution_reporting.runtime.models.reporting import (
    REPORT_MODULE_IDS,
    SpecialTopicPlan,
)

from .handoff_docx import HandoffDocxCore
from .rendered_docx_validator import validate_rendered_markdown_docx

_CITATION_TOKEN = re.compile(r"\[\[CITE:(\d+)\]\]")
_PHOTO_TOKEN = re.compile(r"^\[\[PHOTO:([^]]+)\]\]$")
_FIXED_DOCX_TIME = datetime(2000, 1, 1, tzinfo=UTC)
# Photos sit in the 「现场图片」 column of the problem table, as in the expert
# report; the box matches the renderer's photo-cell size.
_PHOTO_MAX_WIDTH_CM = 3.0
_PHOTO_MAX_HEIGHT_CM = 3.0
_PHOTO_COLUMN_TOKENS = ("图片", "照片")
_PHOTO_ID = re.compile(r"P-\d+")
_CELL_PHOTO_TOKEN = re.compile(r"\[\[PHOTO:([^\]]+)\]\]")


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
    claim_ids: list[str] = Field(default_factory=list)
    submodule_id: str | None = None


class ApprovedReport(StrictModel):
    title: str = Field(min_length=1)
    assessment_background: str = Field(min_length=1)
    findings_overview: str = Field(min_length=1)
    regional_executive_summary: str = Field(min_length=1)
    module_narratives: dict[str, str]
    risk_summary: str = Field(min_length=1)
    improvement_action_plan: str = Field(min_length=1)
    special_topic_plan: SpecialTopicPlan | None = None
    special_topic_analysis: str | None = Field(default=None, min_length=1)
    ledger: ClaimLedger
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
        if (self.special_topic_plan is None) != (self.special_topic_analysis is None):
            raise ValueError(
                "special_topic_plan and special_topic_analysis must either both be present "
                "or both be absent"
            )
        if self.special_topic_plan is not None and self.special_topic_analysis is not None:
            self.special_topic_plan.validate_analysis(self.special_topic_analysis)
        claim_modules = {claim.module_id for claim in self.ledger.claims}
        if claim_modules != set(REPORT_MODULE_IDS):
            raise ValueError("approved report claim ledger requires exactly modules 2.1-2.5")
        source_ids = {source.id for source in self.ledger.sources}
        claim_ids = {claim.id for claim in self.ledger.claims}
        claims_by_id = {claim.id: claim for claim in self.ledger.claims}
        for photo in self.photos:
            if photo.source_id not in source_ids:
                raise ValueError(f"photo {photo.id} references unknown project source")
            unknown_claims = sorted(set(photo.claim_ids) - claim_ids)
            if unknown_claims:
                raise ValueError(f"photo {photo.id} references unknown claims: {unknown_claims}")
            if photo.submodule_id is not None:
                if photo.submodule_id not in {
                    submodule_id
                    for module in REPORT_TAXONOMY.values()
                    for submodule_id in module.submodules
                }:
                    raise ValueError(
                        f"photo {photo.id} references unknown submodule {photo.submodule_id}"
                    )
                if photo.claim_ids and not any(
                    claims_by_id[claim_id].submodule_id == photo.submodule_id
                    for claim_id in photo.claim_ids
                ):
                    raise ValueError(
                        f"photo {photo.id} submodule does not match its linked claims"
                    )
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
    validation_warnings: list[str] = Field(default_factory=list)


class PdsDocxRenderer:
    def __init__(self, handoff_core: HandoffDocxCore):
        self.handoff_core = handoff_core

    def render(
        self,
        report: ApprovedReport,
        output_path: Path,
        *,
        approved_markdown: str | None = None,
        before_publish: Callable[[], None] | None = None,
    ) -> PdsRenderResult:
        """Render approved prose only and validate semantic preservation."""

        cited_markdown = (
            approved_markdown
            if approved_markdown is not None
            else report.ledger.bind_citations(self._compose_markdown(report))
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
            validation_warnings = validate_rendered_markdown_docx(
                temporary_path,
                cited_markdown,
                expected_title=report.title,
                reject_unresolved_tokens=True,
            )
            if before_publish is not None:
                before_publish()
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
            protected_prose_verified=not validation_warnings,
            validation_warnings=validation_warnings,
        )

    @staticmethod
    def _compose_markdown(report: ApprovedReport) -> str:
        photo_ids = {photo.id for photo in report.photos}
        module_narratives = {
            module_id: PdsDocxRenderer._place_photo_tokens(
                strip_leading_module_heading(report.module_narratives[module_id], module_id),
                photo_ids,
            )
            for module_id in REPORT_MODULE_IDS
        }
        return compose_canonical_markdown(
            CanonicalReportContent(
                title=report.title,
                assessment_background=report.assessment_background,
                findings_overview=report.findings_overview,
                regional_executive_summary=report.regional_executive_summary,
                module_narratives=module_narratives,
                risk_summary=report.risk_summary,
                improvement_action_plan=report.improvement_action_plan,
                special_topic_plan=report.special_topic_plan,
                special_topic_analysis=report.special_topic_analysis,
                tables=[
                    CanonicalMarkdownTable(
                        title=table.title,
                        headers=table.headers,
                        rows=table.rows,
                        source_ids=table.source_ids,
                    )
                    for table in report.tables
                ],
            )
        )

    @staticmethod
    def _strip_leading_module_heading(narrative: str, module_id: str) -> str:
        """Remove the canonical module heading even after an editor transition."""

        return strip_leading_module_heading(narrative, module_id)

    @staticmethod
    def _place_photo_tokens(narrative: str, photo_ids: set[str]) -> str:
        """Turn photo ids in a table's 「现场图片」 column into image tokens.

        Authors list the P- ids of the photos that belong to a problem row in
        that row's photo column.  Only ids of report photos become images; any other text in the cell is left as written.
        Photos no table row names are not placed in the body; the evidence
        index lists every photo.
        """

        if not photo_ids:
            return narrative
        output: list[str] = []
        photo_columns: list[int] = []
        in_table = False
        for line in narrative.splitlines():
            stripped = line.strip()
            if not stripped.startswith("|"):
                in_table = False
                output.append(line)
                continue
            cells = stripped.strip("|").split("|")
            if not in_table:
                in_table = True
                photo_columns = [
                    index
                    for index, cell in enumerate(cells)
                    if any(token in cell for token in _PHOTO_COLUMN_TOKENS)
                ]
                output.append(line)
                continue
            if not photo_columns or all(set(cell.strip()) <= set("-: ") for cell in cells):
                output.append(line)
                continue
            for index in photo_columns:
                if index >= len(cells):
                    continue
                ids = _PHOTO_ID.findall(cells[index])
                if ids and all(photo_id in photo_ids for photo_id in ids):
                    cells[index] = " " + " ".join(f"[[PHOTO:{photo_id}]]" for photo_id in ids) + " "
            output.append("|" + "|".join(cells) + "|")
        return "\n".join(output)

    @staticmethod
    def _markdown_table(table: ReportTable) -> str:
        return markdown_table(
            CanonicalMarkdownTable(
                title=table.title,
                headers=table.headers,
                rows=table.rows,
                source_ids=table.source_ids,
            )
        )

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
        """Replace photo tokens in table cells with the photos themselves."""

        photos = {photo.id: photo for photo in report.photos}
        for table in document.tables:
            for row in table.rows:
                for cell in row.cells:
                    photo_ids = _CELL_PHOTO_TOKEN.findall(cell.text)
                    if not photo_ids or any(photo_id not in photos for photo_id in photo_ids):
                        continue
                    for photo_id in photo_ids:
                        if not photos[photo_id].path.is_file():
                            raise FileNotFoundError(
                                f"report photo not found: {photos[photo_id].path}"
                            )
                    paragraphs = cell.paragraphs
                    for paragraph in paragraphs[1:]:
                        paragraph._p.getparent().remove(paragraph._p)
                    first = paragraphs[0]
                    for run in list(first.runs):
                        run._r.getparent().remove(run._r)
                    first.alignment = WD_ALIGN_PARAGRAPH.CENTER
                    cell.vertical_alignment = WD_CELL_VERTICAL_ALIGNMENT.CENTER
                    for index, photo_id in enumerate(photo_ids):
                        paragraph = first if index == 0 else cell.add_paragraph()
                        paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
                        paragraph.add_run().add_picture(
                            str(photos[photo_id].path),
                            **PdsDocxRenderer._photo_fit_dimensions(photos[photo_id].path),
                        )

    @staticmethod
    def _photo_fit_dimensions(path: Path) -> dict[str, object]:
        with Image.open(path) as source:
            image = ImageOps.exif_transpose(source)
            pixel_width, pixel_height = image.size
        if pixel_width <= 0 or pixel_height <= 0:
            return {"width": Cm(_PHOTO_MAX_WIDTH_CM)}
        aspect = pixel_width / pixel_height
        box_aspect = _PHOTO_MAX_WIDTH_CM / _PHOTO_MAX_HEIGHT_CM
        if aspect >= box_aspect:
            return {"width": Cm(_PHOTO_MAX_WIDTH_CM)}
        return {"height": Cm(_PHOTO_MAX_HEIGHT_CM)}

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

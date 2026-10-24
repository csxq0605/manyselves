"""Deterministic validation and assembly of editor-selected report assets."""

from __future__ import annotations

import re
from pathlib import Path

from manyselves.capabilities.distribution_reporting.domain.photo_bindings import (
    runtime_photo_ids,
)
from manyselves.capabilities.distribution_reporting.domain.taxonomy import (
    REPORT_TAXONOMY,
)
from manyselves.capabilities.distribution_reporting.runtime.models.agentic import (
    ClaimRecord,
    EditedReportSubmission,
)
from manyselves.capabilities.distribution_reporting.runtime.models.reporting import (
    EvidenceItem,
    PhotoAsset,
    SpecialTopicPlan,
)
from manyselves.capabilities.distribution_reporting.runtime.rendering.pds_docx_renderer import (
    ReportPhoto,
    ReportTable,
)


def approved_module_marker(module_id: str) -> str:
    return f"[[APPROVED_MODULE:{module_id}]]"


def expand_approved_module_markers(
    edited: EditedReportSubmission, source_modules: dict[str, str]
) -> EditedReportSubmission:
    """Assemble approved prose after editing so the model never re-emits it."""

    narratives = dict(edited.module_narratives)
    for module_id, source in source_modules.items():
        marker = approved_module_marker(module_id)
        if marker in narratives[module_id]:
            narratives[module_id] = narratives[module_id].replace(marker, source)
    return edited.model_copy(update={"module_narratives": narratives})


def validate_final_report_markdown(
    markdown: str,
    special_topic_plan: SpecialTopicPlan | None = None,
) -> dict[str, list[str]]:
    """Enforce the fixed report heading structure."""

    expected: list[tuple[str, int]] = [
        ("1. 配电评估概述", 2),
        ("1.1 评估背景", 3),
        ("1.2 健康度总览", 3),
        ("1.3 各区域执行摘要", 3),
        ("2. 评估内容描述", 2),
    ]
    for module_id, definition in REPORT_TAXONOMY.items():
        expected.append((f"{module_id} {definition.title}", 3))
        for section_id, section in definition.sections.items():
            title = f"{section_id} {section.title}"
            expected.append((title, section_id.count(".") + 2))
    if special_topic_plan is not None:
        special_topic_titles = [
            f"{section.section_id} {section.title}"
            for section in special_topic_plan.sections
        ]
    else:
        special_topic_titles = []
    tail = [
        ("3. 结论与建议", 2),
        ("3.1 风险/问题汇总与概览", 3),
        ("3.2 改善行动速查表", 3),
    ]
    if special_topic_plan is not None:
        tail.extend(
            [
                ("4. 专项问题分析", 2),
                *((title, 3) for title in special_topic_titles),
            ]
        )
    expected.extend(tail)
    lines = markdown.splitlines()
    parsed: list[tuple[int, str, int]] = []
    for line_number, line in enumerate(lines):
        match = re.match(r"^(#{1,6})\s+(.+?)\s*$", line)
        if match:
            parsed.append((len(match.group(1)), match.group(2), line_number))
    by_title: dict[str, list[tuple[int, int]]] = {}
    for level, title, line_number in parsed:
        by_title.setdefault(title, []).append((level, line_number))

    errors: list[str] = []
    expected_titles = {title for title, _ in expected}
    planned_special_topic_ids = {
        section.section_id for section in special_topic_plan.sections
    } if special_topic_plan is not None else set()

    def allowed_special_topic_descendant(title: str) -> bool:
        match = re.match(r"^(4(?:\.\d+){2,})\.?\s+", title)
        if match is None:
            return False
        parts = match.group(1).split(".")
        return ".".join(parts[:2]) in planned_special_topic_ids

    unexpected_numbered = [
        title
        for _level, title, _line_number in parsed
        if re.match(r"^\d+(?:\.\d+)*\.?\s+", title)
        and title not in expected_titles
        and not allowed_special_topic_descendant(title)
    ]
    if unexpected_numbered:
        errors.append(f"unexpected numbered headings={unexpected_numbered}")
    if special_topic_plan is not None:
        chapter_four = by_title.get("4. 专项问题分析", [])
        if len(chapter_four) == 1:
            _chapter_level, chapter_start = chapter_four[0]
            try:
                special_topic_plan.validate_analysis(
                    "\n".join(lines[chapter_start:]),
                    allow_chapter_heading=True,
                )
            except ValueError as exc:
                errors.append(str(exc))
    if special_topic_plan is None and any(
        title == "4. 专项问题分析" or re.match(r"^4\.[1-9][0-9]*\s+", title)
        for _level, title, _line_number in parsed
    ):
        errors.append("Chapter 4 must be absent when no non-empty special-topic plan exists")
    positions: list[int] = []
    for title, expected_level in expected:
        occurrences = by_title.get(title, [])
        if len(occurrences) != 1:
            errors.append(f"{title}: count={len(occurrences)}")
            continue
        actual_level, line_number = occurrences[0]
        if actual_level != expected_level:
            errors.append(
                f"{title}: heading_level={actual_level}, expected={expected_level}"
            )
        positions.append(line_number)
    if positions != sorted(positions):
        errors.append("fixed headings are out of order")

    if errors:
        raise ValueError(
            "final report Markdown integrity failed; "
            f"heading_errors={errors}"
        )
    return {}


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
        photo_ids = runtime_photo_ids(evidence, photos)
        if edited.photo_ids != photo_ids:
            raise ValueError(
                "edited report photo_ids must equal the runtime-owned source-table photo set"
            )
        for photo_id in photo_ids:
            asset = photo_by_id.get(photo_id)
            if asset is None:
                raise ValueError(f"editor selected unknown photo {photo_id}")
            path = (self.workspace / asset.path).resolve()
            if not path.is_relative_to(self.workspace) or not path.is_file():
                raise ValueError(f"photo {photo_id} is not a project-local file")
            candidates = [item for item in evidence if photo_id in item.photo_refs]
            bindings: list[tuple[EvidenceItem, list[str]]] = []
            for item in candidates:
                if item.submodule_id is None:
                    continue
                linked_claim_ids = sorted(
                    claim.id for claim in claims if item.id in claim.source_ids
                )
                matching_claim_ids = [
                    claim_id
                    for claim_id in linked_claim_ids
                    if claim_by_id[claim_id].submodule_id == item.submodule_id
                ]
                bindings.append((item, matching_claim_ids))
            if not bindings:
                raise ValueError(
                    f"photo {photo_id} requires source-table evidence and a smallest submodule"
                )
            # The primary evidence may be one the authors did not place; the
            # photo then goes with the first evidence that has a section.
            source, claim_ids = next(
                (
                    binding
                    for binding in bindings
                    if binding[0].id == asset.primary_evidence_id
                ),
                bindings[0],
            )
            if source.id not in evidence_by_id:
                raise AssertionError("asset binding indexes are inconsistent")
            report_photos.append(
                ReportPhoto(
                    id=photo_id,
                    path=path,
                    caption=f"{source.subject}：{source.fact}",
                    source_id=source.id,
                    claim_ids=claim_ids,
                    submodule_id=source.submodule_id,
                )
            )
        return tables, report_photos

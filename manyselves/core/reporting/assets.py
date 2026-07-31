"""Deterministic validation and assembly of editor-selected report assets."""

from __future__ import annotations

import re
from pathlib import Path

from .agentic_models import ClaimRecord, EditedReportSubmission, ModuleSubmission
from .models import EvidenceItem, PhotoAsset, SpecialTopicPlan
from .rendering.pds_docx_renderer import ReportPhoto, ReportTable
from .taxonomy import REPORT_TAXONOMY, compose_module_markdown


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


def _normalized_prose(value: str) -> str:
    """Normalize only presentation whitespace so approved wording stays testable."""

    return re.sub(r"\s+", "", value)


def _normalized_markdown_content(value: str) -> str:
    """Compare approved wording independently from deterministic heading levels."""

    return _normalized_prose(re.sub(r"^#{1,6}\s+", "", value, flags=re.MULTILINE))


def _normalized_submodule_body(value: str, submodule_id: str) -> str:
    """Exclude the model-authored heading because taxonomy owns canonical titles."""

    lines = value.splitlines()
    first_content = next(
        (index for index, line in enumerate(lines) if line.strip()), None
    )
    if first_content is not None and re.match(
        rf"^#{{1,6}}\s+{re.escape(submodule_id)}(?:\.|\s|$)",
        lines[first_content].strip(),
    ):
        del lines[first_content]
    return _normalized_markdown_content("\n".join(lines))


def _markdown_headings(value: str) -> list[str]:
    return [
        match.group(1).strip()
        for line in value.splitlines()
        if (match := re.match(r"^#{1,6}\s+(.+?)\s*$", line))
    ]


def validate_editor_protection(edited: EditedReportSubmission, claims: list[ClaimRecord]) -> None:
    """Require exact Claim protection and preserved Claim markers."""

    claim_by_id = {claim.id: claim for claim in claims}
    if len(claim_by_id) != len(claims):
        raise ValueError("approved claims contain duplicate ids")
    protected = set(edited.protected_claim_ids)
    expected = set(claim_by_id)
    if protected != expected:
        missing = sorted(expected - protected)
        extra = sorted(protected - expected)
        raise ValueError(f"protected claim set mismatch: missing={missing}, extra={extra}")

    for claim in claims:
        if not claim.footnote_required or not claim.source_ids:
            continue
        marker = f"[[CLAIM:{claim.id}]]"
        narrative = edited.module_narratives[claim.module_id]
        if narrative.count(marker) != 1:
            raise ValueError(
                f"citation marker for {claim.id} must occur exactly once in module "
                f"{claim.module_id}"
            )


def validate_editor_quality(
    edited: EditedReportSubmission,
    modules: dict[str, ModuleSubmission],
) -> list[str]:
    """Require additive synthesis: approved specialist prose may not be deleted."""

    missing_modules = sorted(set(REPORT_TAXONOMY) - set(modules))
    if missing_modules:
        raise ValueError(f"quality validation is missing approved modules: {missing_modules}")
    for module_id, definition in REPORT_TAXONOMY.items():
        narrative = edited.module_narratives[module_id]
        missing = [
            submodule_id
            for submodule_id, submodule in definition.submodules.items()
            if submodule_id not in narrative or submodule.title not in narrative
        ]
        if missing:
            raise ValueError(
                f"chief editor omitted fixed submodules from {module_id}: {missing}"
            )
        normalized_narrative = _normalized_markdown_content(narrative)
        omitted_approved = [
            submodule_id
            for submodule_id, text in modules[module_id].submodule_narratives.items()
            if _normalized_submodule_body(text, submodule_id)
            not in normalized_narrative
        ]
        if omitted_approved:
            raise ValueError(
                f"chief editor deleted or rewrote approved prose from {module_id}: "
                f"{omitted_approved}"
            )
    return editor_quality_observations(edited)


def validate_module_markdown_consistency(
    modules: dict[str, ModuleSubmission],
    exported_markdown: dict[str, str] | None = None,
) -> None:
    """Audit structured submissions and persisted Markdown as one canonical body."""

    missing_modules = sorted(set(REPORT_TAXONOMY) - set(modules))
    if missing_modules:
        raise ValueError(f"module Markdown audit is missing modules: {missing_modules}")

    structured_mismatches: list[str] = []
    export_mismatches: list[str] = []
    missing_export_sections: dict[str, list[str]] = {}
    for module_id, definition in REPORT_TAXONOMY.items():
        module = modules[module_id]
        canonical = compose_module_markdown(module_id, module.submodule_narratives)
        if module.markdown.strip() != canonical.strip():
            structured_mismatches.append(module_id)
        if exported_markdown is None:
            continue
        exported = exported_markdown.get(module_id, "")
        if exported.strip() != canonical.strip():
            export_mismatches.append(module_id)
        missing = [
            section_id
            for section_id in definition.sections
            if re.search(
                rf"^#{{1,6}}\s+{re.escape(section_id)}(?:\.|\s|$)",
                exported,
                flags=re.MULTILINE,
            )
            is None
        ]
        if missing:
            missing_export_sections[module_id] = missing

    if structured_mismatches or export_mismatches or missing_export_sections:
        raise ValueError(
            "module Markdown integrity validation failed; "
            f"structured_mismatches={structured_mismatches}; "
            f"export_mismatches={export_mismatches}; "
            f"missing_export_sections={missing_export_sections}"
        )


def validate_existing_markdown_modules(source_modules: dict[str, str]) -> None:
    """Require the fixed module/submodule structure for Markdown aggregation."""

    missing_modules = sorted(set(REPORT_TAXONOMY) - set(source_modules))
    if missing_modules:
        raise ValueError(f"existing Markdown set is missing modules: {missing_modules}")

    missing_sections: list[str] = []
    for module_id, definition in REPORT_TAXONOMY.items():
        markdown = source_modules[module_id]
        for submodule_id in definition.sections:
            match = re.search(
                rf"^#{{1,6}}\s+{re.escape(submodule_id)}(?:\.)?\s+.+$",
                markdown,
                flags=re.MULTILINE,
            )
            if match is None:
                missing_sections.append(submodule_id)

    if missing_sections:
        raise ValueError(
            "existing Markdown modules are missing fixed sections; "
            f"missing_sections={sorted(missing_sections)}"
        )


def validate_final_report_markdown(
    markdown: str,
    special_topic_plan: SpecialTopicPlan | None = None,
) -> dict[str, list[str]]:
    """Enforce structure and return non-binding semantic observations."""

    expected: list[tuple[str, int]] = [
        ("1. 配电评估概述", 2),
        ("1.1 评估背景", 3),
        ("1.2 健康度总览", 3),
        ("1.3 各区域执行摘要", 3),
        ("2. 评估内容描述", 2),
    ]
    leaves = {title for title, _ in expected[1:4]}
    for module_id, definition in REPORT_TAXONOMY.items():
        expected.append((f"{module_id} {definition.title}", 3))
        for section_id, section in definition.sections.items():
            title = f"{section_id} {section.title}"
            expected.append((title, section_id.count(".") + 2))
            if section_id in definition.submodules:
                leaves.add(title)
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
        ("3.1.1 风险全景图", 4),
        ("3.1.2 各维度风险分析", 4),
        ("3.1.3 数据缺口分析", 4),
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
    leaves.update(
        {
            "3.1.1 风险全景图",
            "3.1.2 各维度风险分析",
            "3.1.3 数据缺口分析",
            "3.2 改善行动速查表",
        }
    )
    leaves.update(special_topic_titles)

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
    unexpected_numbered = [
        title
        for _level, title, _line_number in parsed
        if re.match(r"^\d+(?:\.\d+)*\.?\s+", title)
        and title not in expected_titles
    ]
    if unexpected_numbered:
        errors.append(f"unexpected numbered headings={unexpected_numbered}")
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

    shallow: list[str] = []
    pointer_only: list[str] = []
    for title in leaves:
        occurrences = by_title.get(title, [])
        if len(occurrences) != 1:
            continue
        level, start = occurrences[0]
        end = len(lines)
        for candidate_level, _candidate_title, candidate_line in parsed:
            if candidate_line > start and candidate_level <= level:
                end = candidate_line
                break
        body = "\n".join(lines[start + 1 : end]).strip()
        compact = _normalized_prose(body)
        if len(compact) < 60:
            shallow.append(title)
        if len(compact) < 180 and any(
            marker in body
            for marker in ("详见", "参见", "见汇总表", "见第二章", "见2.")
        ):
            pointer_only.append(title)
    if errors:
        raise ValueError(
            "final report Markdown integrity failed; "
            f"heading_errors={errors}; shallow_sections={sorted(shallow)}; "
            f"pointer_only_sections={sorted(pointer_only)}"
        )
    return {
        "shallow_sections": sorted(shallow),
        "pointer_only_sections": sorted(pointer_only),
    }


def find_shallow_submodules(modules: dict[str, ModuleSubmission]) -> list[str]:
    """Return sections missing a usable reasoning-to-action narrative."""

    reasoning_terms = ("导致", "影响", "风险", "可能", "表明", "原因", "需结合", "使得")
    risk_terms = ("风险", "影响", "后果", "恶化", "失效")
    action_terms = ("建议", "应", "需", "核验", "监测", "复核", "整改", "验证", "排查")
    boundary_terms = ("证据不足", "暂无", "未评估", "待核验", "待补充")
    shallow: list[str] = []
    for module in modules.values():
        for submodule_id, text in module.submodule_narratives.items():
            compact = _normalized_prose(text)
            has_reasoning = any(term in text for term in reasoning_terms)
            has_risk = any(term in text for term in risk_terms)
            has_action = any(term in text for term in action_terms)
            bounded_unknown = any(term in text for term in boundary_terms) and has_action
            if (
                len(compact) < 80
                or not has_risk
                or not ((has_reasoning and has_action) or bounded_unknown)
            ):
                shallow.append(submodule_id)
    return shallow


def editor_quality_observations(edited: EditedReportSubmission) -> list[str]:
    """Return heuristic reviewer signals without accepting or rejecting prose."""

    observations: list[str] = []
    sections = {
        "assessment_background": edited.assessment_background,
        "findings_overview": edited.findings_overview,
        "regional_executive_summary": edited.regional_executive_summary,
        "risk_panorama": edited.risk_panorama,
        "dimension_risk_analysis": edited.dimension_risk_analysis,
        "data_gap_analysis": edited.data_gap_analysis,
        "improvement_action_plan": edited.improvement_action_plan,
    }
    minimum_lengths = {
        "assessment_background": 160,
        "findings_overview": 260,
        "regional_executive_summary": 260,
        "risk_panorama": 320,
        "dimension_risk_analysis": 420,
        "data_gap_analysis": 220,
        "improvement_action_plan": 320,
    }
    if edited.special_topic_plan is not None and edited.special_topic_analysis is not None:
        sections["special_topic_analysis"] = edited.special_topic_analysis
        minimum_lengths["special_topic_analysis"] = (
            260 * len(edited.special_topic_plan.sections)
        )
    observations.extend(
        f"{name}:length_below_guideline"
        for name, minimum in minimum_lengths.items()
        if len(_normalized_prose(sections[name])) < minimum
    )

    dimensions = ("系统架构", "电能质量", "保护", "设备", "运维")
    if sum(term in edited.dimension_risk_analysis for term in dimensions) < 4:
        observations.append("dimension_risk_analysis:few_named_dimensions")
    if not any(
        term in edited.dimension_risk_analysis
        for term in ("共同", "叠加", "放大", "依赖", "制约", "传播")
    ):
        observations.append("dimension_risk_analysis:no_comparison_term")
    if not any(
        term in edited.data_gap_analysis
        for term in ("影响", "限制", "无法", "置信", "优先")
    ):
        observations.append("data_gap_analysis:no_decision_impact_term")

    action_groups = {
        "ownership": ("责任", "牵头", "负责", "接口"),
        "sequence": ("优先", "先", "依赖", "阶段"),
        "acceptance": ("验收", "指标", "验证", "复测"),
    }
    observations.extend(
        f"improvement_action_plan:missing_{group}"
        for group, terms in action_groups.items()
        if not any(term in edited.improvement_action_plan for term in terms)
    )

    return observations


def validate_aggregate_retention(
    edited: EditedReportSubmission,
    source_modules: dict[str, str],
) -> list[str]:
    """Keep aggregation additive: source module reports remain present verbatim."""

    for module_id in REPORT_TAXONOMY:
        source = source_modules[module_id]
        narrative = edited.module_narratives[module_id]
        missing_headings = [
            heading
            for heading in _markdown_headings(source)
            if heading not in narrative
        ]
        if missing_headings:
            raise ValueError(
                f"aggregate editor omitted headings from {module_id}: {missing_headings}"
            )
        if _normalized_prose(source) not in _normalized_prose(narrative):
            raise ValueError(
                f"aggregate editor deleted or rewrote approved module {module_id}"
            )
    return editor_quality_observations(edited)


class ReportAssetAssembler:
    """Resolve typed editor selections into renderer assets with project traceability."""

    def __init__(self, workspace: Path):
        self.workspace = Path(workspace).resolve()

    @staticmethod
    def runtime_photo_ids(
        evidence: list[EvidenceItem],
        photos: list[PhotoAsset],
    ) -> list[str]:
        """Return every source-table photo in manifest order or reject an orphan."""

        evidence_by_photo: dict[str, list[EvidenceItem]] = {}
        for item in evidence:
            if item.submodule_id is None:
                continue
            for photo_id in item.photo_refs:
                evidence_by_photo.setdefault(photo_id, []).append(item)
        orphaned = [photo.id for photo in photos if photo.id not in evidence_by_photo]
        if orphaned:
            raise ValueError(
                "project photos require source-table evidence and a smallest submodule: "
                f"{orphaned}"
            )
        invalid_primary_bindings = {}
        for photo in photos:
            candidates = evidence_by_photo[photo.id]
            candidate_ids = [item.id for item in candidates]
            primary_id = photo.primary_evidence_id
            # Legacy immutable preparation snapshots predate the explicit
            # field. They retain mapper/source-table evidence order, which is
            # the compatibility fallback; new runs persist the chosen ID.
            if primary_id is None:
                continue
            if candidate_ids.count(primary_id) != 1:
                invalid_primary_bindings[photo.id] = {
                    "primary_evidence_id": primary_id,
                    "candidate_evidence_ids": candidate_ids,
                }
        if invalid_primary_bindings:
            raise ValueError(
                "project photo primary evidence binding must reference exactly one "
                f"candidate: {invalid_primary_bindings}"
            )
        return [photo.id for photo in photos]

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
        runtime_photo_ids = self.runtime_photo_ids(evidence, photos)
        if edited.photo_ids != runtime_photo_ids:
            raise ValueError(
                "edited report photo_ids must equal the runtime-owned source-table photo set"
            )
        for photo_id in runtime_photo_ids:
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
            if asset.primary_evidence_id is None:
                source, claim_ids = bindings[0]
            else:
                source, claim_ids = next(
                    binding
                    for binding in bindings
                    if binding[0].id == asset.primary_evidence_id
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

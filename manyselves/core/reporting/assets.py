"""Deterministic validation and assembly of editor-selected report assets."""

from __future__ import annotations

from pathlib import Path

import re

from .agentic_models import ClaimRecord, EditedReportSubmission, ModuleSubmission
from .models import EvidenceItem, PhotoAsset
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
            submodule_id
            for submodule_id in definition.submodules
            if re.search(
                rf"^#{{1,6}}\s+{re.escape(submodule_id)}(?:\.|\s|$)",
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
        for submodule_id in definition.submodules:
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
        for submodule_id, submodule in definition.submodules.items():
            title = f"{submodule_id} {submodule.title}"
            expected.append((title, 4))
            leaves.add(title)
    tail = [
        ("3. 结论与建议", 2),
        ("3.1 风险/问题汇总与概览", 3),
        ("3.1.1 风险全景图", 4),
        ("3.1.2 各维度风险分析", 4),
        ("3.1.3 跨领域关联风险", 4),
        ("3.1.4 数据缺口分析", 4),
        ("3.2 改善行动速查表", 3),
        ("4. 专项问题分析", 2),
        ("4.1 新工厂建厂时规划建议", 3),
        ("4.2 增容建议", 3),
        ("4.3 日常用电管理建议", 3),
        ("4.4 应急管理及合规性管理建议", 3),
    ]
    expected.extend(tail)
    leaves.update(title for title, _ in [*tail[2:7], *tail[8:]])

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
    analysis = edited.cross_module_analysis
    if len(_normalized_prose(analysis)) < 240:
        observations.append("cross_module_analysis:length_below_240")
    mentioned = {module_id for module_id in REPORT_TAXONOMY if module_id in analysis}
    if len(mentioned) < 4:
        observations.append("cross_module_analysis:fewer_than_four_module_refs")
    relation_hits = sum(
        analysis.count(term)
        for term in ("导致", "进而", "叠加", "耦合", "共同", "放大", "传播", "依赖")
    )
    if relation_hits < 3:
        observations.append("cross_module_analysis:few_relation_terms")
    if not any(term in analysis for term in ("建议", "优先", "联合整改", "组合实施")):
        observations.append("cross_module_analysis:no_joint_action_term")
    if not any(term in analysis for term in ("验收", "复核", "指标", "验证")):
        observations.append("cross_module_analysis:no_verification_term")

    sections = {
        "assessment_background": edited.assessment_background,
        "findings_overview": edited.findings_overview,
        "regional_executive_summary": edited.regional_executive_summary,
        "risk_panorama": edited.risk_panorama,
        "dimension_risk_analysis": edited.dimension_risk_analysis,
        "data_gap_analysis": edited.data_gap_analysis,
        "improvement_action_plan": edited.improvement_action_plan,
        "new_factory_planning": edited.new_factory_planning,
        "capacity_expansion_plan": edited.capacity_expansion_plan,
        "daily_power_management": edited.daily_power_management,
        "emergency_compliance_management": edited.emergency_compliance_management,
    }
    minimum_lengths = {
        "assessment_background": 160,
        "findings_overview": 260,
        "regional_executive_summary": 260,
        "risk_panorama": 320,
        "dimension_risk_analysis": 420,
        "data_gap_analysis": 220,
        "improvement_action_plan": 320,
        "new_factory_planning": 260,
        "capacity_expansion_plan": 260,
        "daily_power_management": 260,
        "emergency_compliance_management": 260,
    }
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

    special_requirements = {
        "new_factory_planning": (
            ("规划", "设计", "预留"),
            ("校核", "验收", "验证"),
        ),
        "capacity_expansion_plan": (
            ("增容", "容量", "负荷"),
            ("校核", "方案", "验收"),
        ),
        "daily_power_management": (
            ("巡检", "监测", "台账", "维护"),
            ("责任", "闭环", "复测", "指标"),
        ),
        "emergency_compliance_management": (
            ("应急", "合规", "演练", "危险能量"),
            ("责任", "验证", "记录", "复盘"),
        ),
    }
    for field, groups in special_requirements.items():
        text = sections[field]
        if any(not any(term in text for term in group) for group in groups):
            observations.append(f"{field}:missing_section_specific_action_term")
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
        for photo_id in edited.photo_ids:
            asset = photo_by_id.get(photo_id)
            if asset is None:
                raise ValueError(f"editor selected unknown photo {photo_id}")
            path = (self.workspace / asset.path).resolve()
            if not path.is_relative_to(self.workspace) or not path.is_file():
                raise ValueError(f"photo {photo_id} is not a project-local file")
            candidates = sorted(
                (item for item in evidence if photo_id in item.photo_refs),
                key=lambda item: item.id,
            )
            bindings: list[tuple[EvidenceItem, list[str]]] = []
            for item in candidates:
                linked_claim_ids = sorted(
                    claim.id for claim in claims if item.id in claim.source_ids
                )
                if linked_claim_ids:
                    bindings.append((item, linked_claim_ids))
            if not bindings:
                raise ValueError(
                    f"photo {photo_id} requires evidence and at least one linked claim"
                )
            source, claim_ids = bindings[0]
            if source.id not in evidence_by_id or any(
                claim_id not in claim_by_id for claim_id in claim_ids
            ):
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

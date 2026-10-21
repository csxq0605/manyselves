"""Map S4-6 assessment summaries while preserving their derived status."""

import re
from pathlib import Path
from typing import Any

from manyselves.capabilities.distribution_reporting.domain.taxonomy import (
    REPORT_TAXONOMY,
    resolve_submodule,
)
from manyselves.capabilities.distribution_reporting.runtime.models.preparation import (
    MappingGap,
    MappingResult,
)

from .common import cached_formula_workbook, dispimg_refs, make_evidence, unbound_photo_gaps

# The summary sheet's category path joins taxonomy titles with "＞".
_PATH_SEPARATOR = re.compile(r"\s*[＞>]\s*")


def _text(value: Any) -> str:
    return str(value).strip() if value not in (None, "") else ""


def _is_placeholder(value: Any) -> bool:
    return _text(value) in {"", "/", "\\", "--"}


def _known_submodule(value: Any) -> str | None:
    candidate = _text(value)
    if not candidate.startswith(("2.1.", "2.2.", "2.3.", "2.4.", "2.5.")):
        return None
    try:
        resolve_submodule(candidate)
    except ValueError:
        return None
    return candidate


def _route_path(value: Any) -> tuple[str | None, str | None, str | None]:
    """(module, subsection, gap) for a category path, matching titles exactly.

    The path walks the section tree from the module title down.  A path that
    stops at a heading with subsections stays with that module, and its
    author chooses the subsection.  A path whose segments leave the tree goes
    to the subsection its last segment names, when exactly one has that
    title, and the mismatch is recorded.
    """

    path_text = _text(value)
    segments = [segment for segment in _PATH_SEPARATOR.split(path_text) if segment]
    if not segments:
        return None, None, None
    # The path may start at the module or at a heading inside it.
    starts = [
        (module, module.id) for module in REPORT_TAXONOMY.values() if module.title == segments[0]
    ] or [
        (module, section_id)
        for module in REPORT_TAXONOMY.values()
        for section_id, section in module.sections.items()
        if section.title == segments[0]
    ]
    if len(starts) != 1:
        return None, None, None
    module, node = starts[0]
    for segment in segments[1:]:
        child = next(
            (
                section_id
                for section_id, section in module.sections.items()
                if section_id.rsplit(".", 1)[0] == node and section.title == segment
            ),
            None,
        )
        if child is None:
            break
        node = child
    else:
        return module.id, node if node in module.submodules else None, None
    hits = [
        (other.id, section_id)
        for other in REPORT_TAXONOMY.values()
        for section_id, section in other.submodules.items()
        if section.title == segments[-1]
    ]
    if len(hits) == 1:
        module_id, section_id = hits[0]
        return module_id, section_id, (
            f"原表分类路径“{path_text}”与报告小节树的层级不一致；按最后一段归入 {section_id}"
        )
    return module.id, None, (
        f"原表分类路径“{path_text}”有一段对不上报告小节树；归入 {module.id}，由模块作者选定小节"
    )


def map_s4_6(path: Path, *, file_id: str) -> MappingResult:
    path = Path(path)
    with cached_formula_workbook(path) as (workbook, formula_gaps):
        evidence = []
        gaps: list[MappingGap] = list(formula_gaps)
        if "评估信息汇总表" in workbook.sheetnames:
            sheet = workbook["评估信息汇总表"]
            for row in range(4, sheet.max_row + 1):
                submodule_id = _known_submodule(sheet.cell(row, 2).value)
                if not submodule_id:
                    continue
                finding = _text(sheet.cell(row, 6).value)
                recommendation = _text(sheet.cell(row, 7).value)
                if _is_placeholder(finding) and _is_placeholder(recommendation):
                    gaps.append(
                        MappingGap(
                            code="empty_summary_assessment",
                            message=f"{submodule_id} 汇总评估仅填写占位符，不能作为结论证据",
                            sheet=sheet.title,
                            cell=f"F{row}:G{row}",
                            module_id=resolve_submodule(submodule_id).module_id,
                            submodule_id=submodule_id,
                        )
                    )
                    continue
                parts = []
                if not _is_placeholder(finding):
                    parts.append(f"既有评估结论={finding}")
                if not _is_placeholder(recommendation):
                    parts.append(f"既有行动建议={recommendation}")
                fact = "；".join(parts)
                evidence.append(
                    make_evidence(
                        file_id=file_id,
                        path=path,
                        sheet=sheet.title,
                        cell=f"F{row}:G{row}",
                        row=row,
                        column="F",
                        subject=f"评估总表 {submodule_id}",
                        fact=fact,
                        module_id=resolve_submodule(submodule_id).module_id,
                        submodule_id=submodule_id,
                        confidence=0.65,
                        needs_confirmation=True,
                        summary_entry=True,
                    )
                )
        else:
            gaps.append(
                MappingGap(
                    code="missing_sheet",
                    message="缺少评估信息汇总表",
                    sheet="评估信息汇总表",
                )
            )

        if "结论建议汇总表" in workbook.sheetnames:
            sheet = workbook["结论建议汇总表"]
            for row in range(1, sheet.max_row + 1):
                actual_path = _text(sheet.cell(row, 2).value)
                legacy_path = _text(sheet.cell(row, 8).value)
                path_value = actual_path or legacy_path
                observation = _text(sheet.cell(row, 3).value)
                risk = _text(sheet.cell(row, 4).value)
                action = _text(sheet.cell(row, 5).value)
                if not any((observation, risk, action)):
                    continue
                if (
                    actual_path.upper().startswith(("=DISPIMG(", "=_XLFN.DISPIMG("))
                    or "CATEGRY" in actual_path.upper()
                    or observation in {"结论建议汇总表", "发现问题 Topics"}
                ):
                    continue
                module_id, submodule_id, mismatch = _route_path(path_value)
                if module_id is None:
                    # Kept unassigned: the module authors place it.
                    gaps.append(
                        MappingGap(
                            code="unrouted_conclusion",
                            message=(
                                "结论建议的分类路径对不上报告小节树，由模块作者归属："
                                f"{path_value or '未填写分类路径'}"
                            ),
                            sheet=sheet.title,
                            cell=f"B{row}:F{row}",
                        )
                    )
                elif mismatch:
                    gaps.append(
                        MappingGap(
                            code="summary_path_mismatch",
                            message=mismatch,
                            sheet=sheet.title,
                            cell=f"B{row}",
                            module_id=module_id,
                            submodule_id=submodule_id,
                        )
                    )
                fact = "；".join(
                    part
                    for part in (
                        f"既有问题={observation}" if observation else "",
                        f"既有风险={risk}" if risk else "",
                        f"既有建议={action}" if action else "",
                    )
                    if part
                )
                source_cell = f"B{row}:F{row}" if actual_path else f"C{row}:E{row}"
                evidence.append(
                    make_evidence(
                        file_id=file_id,
                        path=path,
                        sheet=sheet.title,
                        cell=source_cell,
                        row=row,
                        column="C",
                        subject=f"评估总表 {submodule_id or module_id or '待归属'}",
                        fact=fact,
                        module_id=module_id,
                        submodule_id=submodule_id,
                        photo_refs=dispimg_refs(sheet.cell(row, 6).value),
                        confidence=0.6,
                        needs_confirmation=True,
                        summary_entry=True,
                    )
                )
        gaps.extend(unbound_photo_gaps(workbook, evidence))
        return MappingResult(evidence_items=evidence, gaps=gaps)

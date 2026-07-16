"""Map S4-6 assessment summaries while preserving their derived status."""

from pathlib import Path
from typing import Any

from openpyxl import load_workbook

from ..taxonomy import REPORT_TAXONOMY, resolve_submodule
from .common import MappingGap, MappingResult, make_evidence


def _text(value: Any) -> str:
    return str(value).strip() if value not in (None, "") else ""


def _known_submodule(value: Any) -> str | None:
    candidate = _text(value)
    if not candidate.startswith(("2.1.", "2.2.", "2.3.", "2.4.", "2.5.")):
        return None
    try:
        resolve_submodule(candidate)
    except ValueError:
        return None
    return candidate


def _submodule_from_path(value: Any) -> str | None:
    path_text = _text(value)
    if not path_text:
        return None
    definitions = sorted(
        (
            definition
            for module in REPORT_TAXONOMY.values()
            for definition in module.submodules.values()
        ),
        key=lambda definition: len(definition.title),
        reverse=True,
    )
    for definition in definitions:
        if definition.title in path_text:
            return definition.id
    return None


def map_s4_6(path: Path, *, file_id: str) -> MappingResult:
    path = Path(path)
    workbook = load_workbook(path, read_only=False, data_only=False)
    try:
        evidence = []
        gaps: list[MappingGap] = []
        if "评估信息汇总表" in workbook.sheetnames:
            sheet = workbook["评估信息汇总表"]
            for row in range(4, sheet.max_row + 1):
                submodule_id = _known_submodule(sheet.cell(row, 2).value)
                if not submodule_id:
                    continue
                finding = _text(sheet.cell(row, 6).value)
                recommendation = _text(sheet.cell(row, 7).value)
                if not finding and not recommendation:
                    continue
                parts = []
                if finding:
                    parts.append(f"既有评估结论={finding}")
                if recommendation:
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
                submodule_id = _submodule_from_path(sheet.cell(row, 8).value)
                if not submodule_id:
                    continue
                observation = _text(sheet.cell(row, 3).value)
                risk = _text(sheet.cell(row, 4).value)
                action = _text(sheet.cell(row, 5).value)
                if not any((observation, risk, action)):
                    continue
                fact = "；".join(
                    part
                    for part in (
                        f"既有问题={observation}" if observation else "",
                        f"既有风险={risk}" if risk else "",
                        f"既有建议={action}" if action else "",
                    )
                    if part
                )
                evidence.append(
                    make_evidence(
                        file_id=file_id,
                        path=path,
                        sheet=sheet.title,
                        cell=f"C{row}:E{row}",
                        row=row,
                        column="C",
                        subject=f"评估总表 {submodule_id}",
                        fact=fact,
                        module_id=resolve_submodule(submodule_id).module_id,
                        submodule_id=submodule_id,
                        confidence=0.6,
                        needs_confirmation=True,
                    )
                )
        return MappingResult(evidence_items=evidence, gaps=gaps)
    finally:
        workbook.close()

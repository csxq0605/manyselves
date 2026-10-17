"""Shared mapper carriers and evidence construction helpers."""

import hashlib
import re
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from openpyxl import Workbook, load_workbook

from manyselves.capabilities.distribution_reporting.runtime.models.preparation import MappingGap
from manyselves.capabilities.distribution_reporting.runtime.models.reporting import (
    EvidenceItem,
    EvidenceMeasurement,
    SourceLocation,
)

_DISPIMG_PATTERN = re.compile(r'DISPIMG\("([^"]+)"', re.IGNORECASE)


def dispimg_refs(*values: Any) -> list[str]:
    refs: list[str] = []
    for value in values:
        if not isinstance(value, str):
            continue
        refs.extend(match.group(1) for match in _DISPIMG_PATTERN.finditer(value))
    return list(dict.fromkeys(refs))


@contextmanager
def cached_formula_workbook(path: Path) -> Iterator[tuple[Workbook, list[MappingGap]]]:
    """Resolve saved scalar results in memory, retaining DISPIMG for photo binding.

    Both views derive dimensions from cells. Missing/error caches remain gaps,
    even if an unresolved routing cell prevents its row from becoming evidence.
    The source workbook is never saved or recalculated.
    """
    workbook = load_workbook(path, read_only=False, data_only=False)
    try:
        values = load_workbook(path, read_only=False, data_only=True)
        gaps = []
        try:
            for sheet in workbook.worksheets:
                cached_sheet = values[sheet.title]
                for row in sheet.iter_rows():
                    for cell in row:
                        if cell.data_type != "f" or dispimg_refs(cell.value):
                            continue
                        cached = cached_sheet.cell(cell.row, cell.column)
                        if cached.value is None or cached.data_type == "e":
                            error = cached.data_type == "e"
                            detail = f"；缓存错误={cached.value}" if error else ""
                            gaps.append(MappingGap(
                                code="formula_result_error" if error else "formula_result_unavailable",
                                message=f"公式缺少可用缓存结果：{sheet.title}!{cell.coordinate}；{cell.value}{detail}",
                                sheet=sheet.title,
                                cell=cell.coordinate,
                            ))
                            cell.value = None
                        else:
                            cell.value = cached.value
        finally:
            values.close()
        yield workbook, gaps
    finally:
        workbook.close()


def evidence_id(
    file_id: str,
    sheet: str,
    cell: str,
    submodule_id: str | None,
    fact: str,
) -> str:
    raw = "\0".join((file_id, sheet, cell, submodule_id or "", fact)).encode()
    return f"ev-{hashlib.sha256(raw).hexdigest()[:20]}"


def make_evidence(
    *,
    file_id: str,
    path: Path,
    sheet: str,
    cell: str,
    row: int,
    column: str,
    subject: str,
    fact: str,
    module_id: str | None = None,
    submodule_id: str | None = None,
    photo_refs: list[str] | None = None,
    value: str | float | int | None = None,
    unit: str | None = None,
    measurements: list[EvidenceMeasurement] | None = None,
    confidence: float = 1.0,
    needs_confirmation: bool = False,
) -> EvidenceItem:
    return EvidenceItem(
        id=evidence_id(file_id, sheet, cell, submodule_id, fact),
        subject=subject,
        fact=fact,
        source=SourceLocation(
            file_id=file_id,
            path=path,
            sheet=sheet,
            cell=cell,
            row=row,
            column=column,
        ),
        value=value,
        unit=unit,
        measurements=measurements or [],
        confidence=confidence,
        needs_confirmation=needs_confirmation,
        module_id=module_id,
        submodule_id=submodule_id,
        photo_refs=photo_refs or [],
    )

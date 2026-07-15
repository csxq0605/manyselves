"""Shared mapper carriers and evidence construction helpers."""

import hashlib
import re
from pathlib import Path
from typing import Any

from ..models import EvidenceItem, ReportingModel, SourceLocation

_DISPIMG_PATTERN = re.compile(r'DISPIMG\("([^"]+)"', re.IGNORECASE)


class MappingGap(ReportingModel):
    code: str
    message: str
    sheet: str | None = None
    cell: str | None = None


class MappingResult(ReportingModel):
    evidence_items: list[EvidenceItem]
    gaps: list[MappingGap]


def dispimg_refs(*values: Any) -> list[str]:
    refs: list[str] = []
    for value in values:
        if not isinstance(value, str):
            continue
        refs.extend(match.group(1) for match in _DISPIMG_PATTERN.finditer(value))
    return list(dict.fromkeys(refs))


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
        confidence=confidence,
        needs_confirmation=needs_confirmation,
        module_id=module_id,
        submodule_id=submodule_id,
        photo_refs=photo_refs or [],
    )

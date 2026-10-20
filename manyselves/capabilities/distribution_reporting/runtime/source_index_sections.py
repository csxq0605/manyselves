"""Judgment basis and data-gap sections of the evidence and source index.

The report body only points to these ("资料未提供，见数据缺口表"); the
index is where the evidence ids, judgments and missing material are listed.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pydantic import BaseModel

_DISPOSITION_TEXT = {
    "concern": "存在问题",
    "no_concern": "未见问题",
    "undetermined": "无法判断",
    "not_applicable": "不适用",
}


def _cell(text: object) -> str:
    return " ".join(str(text or "").split()).replace("|", "／")


def _plain(value: Any) -> Any:
    return value.model_dump(mode="json") if isinstance(value, BaseModel) else value


def judgment_basis_lines(module_submissions: Mapping[str, Any]) -> list[str]:
    lines = ["### 判断依据与边界", ""]
    rows: list[str] = []
    for module_id in sorted(module_submissions):
        module = _plain(module_submissions[module_id]) or {}
        assessments = _plain(module.get("assessments")) or {}
        for section_id in sorted(assessments):
            records = (_plain(assessments[section_id]) or {}).get("records") or []
            for record in records:
                record = _plain(record)
                disposition = str(record.get("disposition") or "")
                rows.append(
                    "| " + " | ".join([
                        section_id,
                        "、".join(record.get("evidence_ids") or []),
                        _DISPOSITION_TEXT.get(disposition, disposition),
                        _cell(record.get("scope")),
                        _cell(record.get("basis")),
                    ]) + " |"
                )
    if not rows:
        return [*lines, "本报告没有登记判断记录。", ""]
    return [
        *lines,
        "| 小节 | 证据 | 判断 | 范围 | 依据与限制 |",
        "| --- | --- | --- | --- | --- |",
        *rows,
        "",
    ]


def load_data_gaps(workspace: Path, run_id: str) -> list[dict[str, Any]]:
    """The registered data gaps of one run, without duplicates."""

    gaps: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for path in sorted((Path(workspace) / f"Work/runs/{run_id}/gaps").glob("*.json")):
        try:
            gap = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        key = (str(gap.get("task_id") or ""), _cell(gap.get("gap")))
        if not key[1] or key in seen:
            continue
        seen.add(key)
        gaps.append(gap)
    return gaps


def data_gap_lines(workspace: Path, run_id: str) -> list[str]:
    lines = ["### 数据缺口表", ""]
    gaps = load_data_gaps(workspace, run_id)
    if not gaps:
        return [*lines, "本报告没有登记资料缺口。", ""]
    rows = [
        "| " + " | ".join([
            f"G{index}",
            _cell(gap.get("gap")),
            _cell(gap.get("impact")),
            _cell(gap.get("requested_input")),
            _cell(str(gap.get("task_id") or "").removeprefix("module-")),
        ]) + " |"
        for index, gap in enumerate(gaps, 1)
    ]
    return [
        *lines,
        "| 编号 | 缺少的资料 | 对判断的影响 | 需要补充 | 提出环节 |",
        "| --- | --- | --- | --- | --- |",
        *rows,
        "",
    ]


def judgment_and_gap_index_markdown(
    workspace: Path,
    run_id: str,
    module_submissions: Mapping[str, Any],
) -> str:
    return "\n".join([
        *judgment_basis_lines(module_submissions),
        *data_gap_lines(workspace, run_id),
    ]).rstrip()

"""Rows on one column of one table are reviewed together, with all that column's records."""

from __future__ import annotations

from typing import Any

from manyselves.capabilities.distribution_reporting.domain.judgment_ledger import JudgmentRow
from manyselves.capabilities.distribution_reporting.runtime.case_review import case_batches
from manyselves.capabilities.distribution_reporting.runtime.context_packs import Sites
from manyselves.capabilities.distribution_reporting.runtime.models.reporting import EvidenceItem


def _item(eid: str, place: str | None, sheet: str, field: str | None, fact: str, *,
          summary: bool = False, reading: float | None = None) -> EvidenceItem:
    return EvidenceItem(
        id=eid, subject=place or "汇总", place=place, fact=fact, checkpoint_field=field,
        summary_entry=summary,
        measurements=[{"quantity": "current", "value": reading, "unit": "A", "label": "剩余电流"}]
        if reading is not None else [],
        source={"file_id": "t", "path": "Inputs/t.xlsx", "sheet": sheet, "row": 1},
    )


ITEMS = [
    _item("E-1", "A房", "合规表", "整机备件", "整机备件=NG"),
    _item("E-2", "B房", "合规表", "整机备件", "整机备件=NG"),
    # Nobody cites it, but it is the exception the reviewer must see.
    _item("E-3", "C房", "合规表", "整机备件", "整机备件=OK"),
    _item("E-4", "A房", "合规表", "安全用具", "安全用具=NG"),
    _item("E-5", None, "汇总表", None, "原评估：建议更换", summary=True),
    _item("E-6", None, "汇总表", None, "原评估：建议检修", summary=True),
    _item("E-7", None, "汇总表", None, "原评估：其他", summary=True),
    _item("E-8", "A房", "详情", "剩余电流", "剩余电流=2.1", reading=2.1),
    _item("E-9", "B房", "详情", "剩余电流", "剩余电流=2.9", reading=2.9),
]


def _row(rid: str, evidence: list[str], disposition: str = "concern") -> JudgmentRow:
    module_id = rid.split("-")[1]
    values: dict[str, Any] = {
        "id": rid, "submodule_id": f"{module_id}.1", "object": "对象", "checkpoint": "检查",
        "disposition": disposition, "evidence_status": "现场确认", "evidence_ids": evidence,
        "basis": "依据",
    }
    if disposition == "concern":
        values.update(risk_level="中", finding="f", hazard="h",
                      action={"measure": "m", "priority": "一般", "timeframe": "一月内"})
    if disposition == "undetermined":
        values.update(gap={"missing": "缺", "impact": "无法判断", "requested_input": "补"})
    return JudgmentRow.model_validate(values)


def _render(items) -> str:
    return "\n".join(f"{item.id} {item.fact}" for item in items)


def test_rows_on_one_column_are_one_case_across_rooms_and_modules() -> None:
    ledger = {
        "2.5": [
            _row("J-2.5-1", ["E-1"]), _row("J-2.5-2", ["E-2"]),
            # A field record wins over the summary entry it also cites.
            _row("J-2.5-3", ["E-4", "E-5"]),
            _row("J-2.5-4", ["E-5"]), _row("J-2.5-5", ["E-6"]),
        ],
        "2.3": [_row("J-2.3-1", ["E-8", "E-9"], "no_concern")],
        "2.4": [_row("J-2.4-1", ["E-8"], "undetermined")],
    }
    (batch,) = case_batches(ledger, Sites(ITEMS), _render, budget_chars=100_000)
    assert [(c["case"], c["row_ids"], c["record_ids"]) for c in batch["cases"]] == [
        ("合规表 「整机备件」栏", ["J-2.5-1", "J-2.5-2"], ["E-1", "E-2", "E-3"]),
        ("汇总表", ["J-2.5-4", "J-2.5-5"], ["E-5", "E-6"]),
        ("详情 「剩余电流」栏 读数：剩余电流", ["J-2.3-1", "J-2.4-1"], ["E-8", "E-9"]),
    ]
    # The lone 安全用具 row is no case; every row and record is sent once.
    assert sorted(r["id"] for r in batch["rows"]) == [
        "J-2.3-1", "J-2.4-1", "J-2.5-1", "J-2.5-2", "J-2.5-4", "J-2.5-5"]
    assert batch["facts"].count("E-8 ") == 1


def test_a_case_is_never_split_across_batches() -> None:
    ledger = {"2.5": [_row(f"J-2.5-{n}", ["E-1" if n % 2 else "E-2"]) for n in range(1, 5)]}
    batches = case_batches(ledger, Sites(ITEMS), _render, budget_chars=10)
    assert len(batches) == 1 and batches[0]["batch_id"] == "C-01"
